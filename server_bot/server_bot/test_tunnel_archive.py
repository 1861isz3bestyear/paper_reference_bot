# Copyright 2026 anchored-vwam-backtest contributors
"""Verify durable raw tunnel evidence and failure isolation."""

from __future__ import annotations

import sqlite3
import zlib
from contextlib import closing
from io import BytesIO
from types import SimpleNamespace
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
    response.raw = BytesIO(b'{"result":{"raw":[1,2]},"code":"00000","data":[]}')
    responses.extend([response] * 22)
    path = tmp_path / "nested" / "archive.sqlite"
    assert collect(path, session) == 1
    assert collect(path, session) == 1
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute(
            "SELECT source, requested_at, available_at, payload, error "
            "FROM tunnel_inputs ORDER BY id"
        ).fetchall()
    expected_rows = 24
    assert len(rows) == expected_rows
    assert rows[0][3] == response.text
    assert rows[0][1] <= rows[0][2]
    assert rows[11][0] == "margin"
    assert rows[11][4] == "MissingMarginCredentialsError"


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
    good.raw = BytesIO(b'{"result":[],"code":"00000","data":[]}')
    responses.extend([bad, *([good] * 10)])
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
        response.raw = BytesIO(
            b'{"code":"00000","data":[],"result":{"preserved":[1,2,3]}}'
        )
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
    assert not (any(row[2:] != (12, 12) for row in groups))
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
        "bitget_growth_24h",
        "bitget_growth_30d",
        "bitget_loan_rate",
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
    payload = (
        '{"code":"00000","data":[],'
        '"result":{"instrument":"BTC-€","data":[1,2,3]}}'
    )
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
    assert groups == [(12,), (12,)]
    assert raw_count == [(12,)]
    assert all(row[1] <= row[2] for row in rows)
    assert rows[11][0::3] == ("margin", "MissingMarginCredentialsError")


@pytest.mark.parametrize("asset", ["BTC", "ETH"])
def test_bitget_requests_match_local_feeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asset: str
) -> None:
    """Save both history windows and the current quote without credentials."""
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
    requests_seen: list[tuple[str, object]] = []
    pauses: list[float] = []
    session = requests.Session()

    def get(url: str, **kwargs: object) -> requests.Response:
        """Return a valid envelope and record public query parameters.

        Returns:
            A successful exchange response.

        """
        requests_seen.append((url, kwargs.get("params")))
        response = requests.Response()
        response.status_code = 200
        response.raw = BytesIO(b'{"result":[],"code":"00000","data":[]}')
        return response

    monkeypatch.setattr(session, "get", get)
    monkeypatch.setattr(
        "server_bot.tunnel_archive.time", SimpleNamespace(sleep=pauses.append)
    )
    assert collect(tmp_path / "archive.sqlite", session, asset=asset) == 1
    prefix = "https://api.bitget.com/api/v3/market/"
    assert requests_seen[-3:] == [
        (
            prefix + "margin-loan-growth",
            {"symbol": f"{asset}USDT", "coin": asset, "period": "24h"},
        ),
        (
            prefix + "margin-loan-growth",
            {"symbol": f"{asset}USDT", "coin": asset, "period": "30d"},
        ),
        (prefix + "margin-loans", {"coin": asset, "level": "vip0"}),
    ]
    assert pauses == [1, 1, 1]


@pytest.mark.parametrize("storage_mode", ["raw", "compact"])
@pytest.mark.parametrize(
    "payload",
    [
        b'{"code":"40000","msg":"rate limited","data":[]}',
        b'{"code":"00000"}',
        b'{"code":"00000","data":null}',
        b'{"result":[]}',
    ],
)
def test_bitget_failures_retain_evidence_and_other_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    storage_mode: Literal["raw", "compact"],
    payload: bytes,
) -> None:
    """Reject Bitget API failures even when HTTP succeeds, in either mode."""
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    pauses: list[float] = []
    monkeypatch.setattr(
        "server_bot.tunnel_archive.time", SimpleNamespace(sleep=pauses.append)
    )
    session = requests.Session()

    def get(url: str, **_kwargs: object) -> requests.Response:
        """Fail Bitget feeds but let other public sources succeed.

        Returns:
            A source-specific exchange response.

        """
        response = requests.Response()
        response.status_code = 200
        response.raw = BytesIO(payload if "bitget" in url else b'{"result":[]}')
        return response

    monkeypatch.setattr(session, "get", get)
    path = tmp_path / "archive.sqlite"
    expected_failures = 4
    assert (
        collect(path, session, storage_mode=storage_mode) == expected_failures
    )
    with closing(sqlite3.connect(path)) as db:
        if storage_mode == "raw":
            rows: list[tuple[str, str]] = db.execute(
                "SELECT payload,error FROM tunnel_inputs "
                "WHERE source LIKE 'bitget_%'"
            ).fetchall()
        else:
            compressed: list[tuple[bytes, str]] = db.execute(
                "SELECT p.payload,o.error FROM tunnel_observations o "
                "JOIN tunnel_payloads p ON p.digest=o.digest "
                "WHERE o.source LIKE 'bitget_%'"
            ).fetchall()
            rows = [
                (zlib.decompress(body).decode(), error)
                for body, error in compressed
            ]
    assert rows == [(payload.decode(), "ValueError")] * 3
