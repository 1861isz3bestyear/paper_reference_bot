# Copyright 2026 anchored-vwam-backtest contributors
"""Verify durable raw tunnel evidence and failure isolation."""

from __future__ import annotations

import sqlite3
import zlib
from contextlib import closing
from io import BytesIO
from typing import TYPE_CHECKING, Literal

import pytest
import requests

from server_bot.tunnel_archive import collect

if TYPE_CHECKING:
    from pathlib import Path


ARCHIVE_SNAPSHOT_COUNT = 2


def test_collection_survives_restart_and_missing_margin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify collection survives restart and missing margin."""
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
    session = requests.Session()
    responses: list[requests.Response] = []

    def get(*_args: object, **_kwargs: object) -> requests.Response:
        """Get.

        Returns:
            The get result.

        """
        return responses.pop(0)

    monkeypatch.setattr(session, "get", get)
    response = requests.Response()
    response.status_code = 200
    response.raw = BytesIO(b'{"result": {"raw": [1, 2]}, "retCode": 0}')
    responses.extend([response] * 16)
    path = tmp_path / "nested" / "archive.sqlite"
    assert collect(path, session) == 1
    assert collect(path, session) == 1
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute(
            "SELECT source, requested_at, available_at, payload, error "
            "FROM tunnel_inputs ORDER BY id"
        ).fetchall()
    expected_rows = 18
    assert len(rows) == expected_rows
    assert rows[0][3] == response.text
    assert rows[0][1] <= rows[0][2]
    assert rows[8][0] == "margin"
    assert rows[8][4] == "ValueError"


@pytest.mark.parametrize("storage_mode", ["raw", "compact"])
def test_exchange_failure_keeps_body_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    storage_mode: Literal["raw", "compact"],
) -> None:
    """Verify exchange failure keeps body and continues."""
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    session = requests.Session()
    responses: list[requests.Response] = []

    def get(*_args: object, **_kwargs: object) -> requests.Response:
        """Get.

        Returns:
            The get result.

        """
        return responses.pop(0)

    monkeypatch.setattr(session, "get", get)
    bad = requests.Response()
    bad.status_code = 200
    bad.raw = BytesIO(b'{"retCode": 10001, "result": {}}')
    good = requests.Response()
    good.status_code = 200
    good.raw = BytesIO(b'{"result": []}')
    responses.extend([bad, good, good, good, good, good, good, good])
    path = tmp_path / "archive.sqlite"
    assert collect(path, session, storage_mode=storage_mode) == (
        ARCHIVE_SNAPSHOT_COUNT
    )
    with closing(sqlite3.connect(path)) as db:
        if storage_mode == "raw":
            rows: list[tuple[str, str]] = db.execute(
                "SELECT payload, error FROM tunnel_inputs"
            ).fetchall()
        else:
            compressed: list[tuple[bytes, str]] = db.execute(
                "SELECT p.payload, o.error FROM tunnel_observations o "
                "JOIN tunnel_payloads p ON p.digest = o.digest ORDER BY o.id"
            ).fetchall()
            rows = [
                (zlib.decompress(body).decode("utf-8"), error)
                for body, error in compressed
            ]
    assert rows[0] == (bad.text, "ValueError")
    assert rows[1] == (good.text, "")


def test_minute_collection_preserves_all_sources_and_legacy_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep replay inputs grouped without inventing timestamps for old data."""
    monkeypatch.setenv("BYBIT_API_KEY", "test-key")
    monkeypatch.setenv("BYBIT_API_SECRET", "test-secret")
    path = tmp_path / "archive.sqlite"
    with closing(sqlite3.connect(path)) as db, db:
        _ = db.execute("""CREATE TABLE tunnel_inputs (
            id INTEGER PRIMARY KEY, asset TEXT NOT NULL, source TEXT NOT NULL,
            requested_at TEXT NOT NULL, available_at TEXT NOT NULL,
            payload TEXT NOT NULL, error TEXT NOT NULL)""")
        _ = db.execute(
            "INSERT INTO tunnel_inputs VALUES (1, 'BTC', 'spot', '', '',"
            " '{}', '')"
        )
    requested: list[tuple[str, object]] = []
    session = requests.Session()

    def get(url: str, **kwargs: object) -> requests.Response:
        """Return distinguishable source payloads.

        Returns:
            A successful exchange response.

        """
        requested.append((url, kwargs.get("params")))
        response = requests.Response()
        response.status_code = 200
        response.raw = BytesIO(b'{"retCode":0,"result":{"preserved":[1,2,3]}}')
        return response

    monkeypatch.setattr(session, "get", get)
    results = (collect(path, session), collect(path, session))
    assert results == (0, 0)
    with closing(sqlite3.connect(path)) as db:
        legacy: list[tuple[object, ...]] = db.execute(
            "SELECT collection_id, minute_at FROM tunnel_inputs WHERE id = 1"
        ).fetchall()
        groups: list[tuple[object, ...]] = db.execute(
            "SELECT collection_id, minute_at, COUNT(*), COUNT(DISTINCT source) "
            "FROM tunnel_inputs WHERE id > 1 GROUP BY collection_id, minute_at"
        ).fetchall()
        sources_saved: list[tuple[str, str, str]] = db.execute(
            "SELECT source, payload, error FROM tunnel_inputs WHERE id > 1"
        ).fetchall()
    assert legacy == [(None, None)]
    expected_passes = 2
    assert len(groups) == expected_passes
    assert not (any(row[2:] != (9, 9) for row in groups))
    assert not any(not str(row[1]).endswith(":00+00:00") for row in groups)
    assert {row[0] for row in sources_saved} == {
        "instruments",
        "options",
        "spot",
        "depth",
        "candles",
        "ticker",
        "hourly_candles",
        "oi",
        "margin",
    }
    assert not any(
        row[2] or '"preserved":[1,2,3]' not in row[1] for row in sources_saved
    )
    assert (
        "https://api.bybit.com/v5/market/kline",
        {
            "category": "linear",
            "symbol": "BTCUSDT",
            "interval": "1",
            "limit": "1000",
        },
    ) in requested
    assert (
        "https://api.bybit.com/v5/market/tickers",
        {"category": "linear", "symbol": "BTCUSDT"},
    ) in requested


def test_compact_snapshots_preserve_payloads_and_deduplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep exact payloads, failures and timing across restarts and modes."""
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
    payload = '{"result":{"instrument":"BTC-€","data":[1,2,3]}}'
    session = requests.Session()

    def get(*_args: object, **_kwargs: object) -> requests.Response:
        """Return a repeatable successful response.

        Returns:
            A UTF-8 exchange response.

        """
        response = requests.Response()
        response.status_code = 200
        response.encoding = "utf-8"
        response.raw = BytesIO(payload.encode("utf-8"))
        return response

    monkeypatch.setattr(session, "get", get)
    path = tmp_path / "archive.sqlite"
    assert collect(path, session, storage_mode="compact") == 1
    assert collect(path, session, storage_mode="compact") == 1
    assert collect(path, session) == 1
    with closing(sqlite3.connect(path)) as db:
        stored: list[tuple[bytes]] = db.execute(
            "SELECT payload FROM tunnel_payloads"
        ).fetchall()
        rows: list[tuple[str, str, str, str]] = db.execute(
            "SELECT source, requested_at, available_at, error "
            "FROM tunnel_observations ORDER BY id"
        ).fetchall()
        groups: list[tuple[int]] = db.execute(
            "SELECT count(*) FROM tunnel_observations GROUP BY collection_id"
        ).fetchall()
        raw_count: list[tuple[int]] = db.execute(
            "SELECT count(*) FROM tunnel_inputs"
        ).fetchall()
    assert {zlib.decompress(row[0]).decode("utf-8") for row in stored} == {
        payload,
        "",
    }
    assert groups == [(9,), (9,)]
    assert raw_count == [(9,)]
    assert all(row[1] <= row[2] for row in rows)
    assert rows[8][0::3] == ("margin", "ValueError")
