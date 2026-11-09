# Copyright 2026 anchored-vwam-backtest contributors
"""Verify durable raw tunnel evidence and failure isolation."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from io import BytesIO
from typing import TYPE_CHECKING

import pytest
import requests

from server_bot.tunnel_archive import collect

if TYPE_CHECKING:
    from pathlib import Path


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
        """Get."""
        return responses.pop(0)

    monkeypatch.setattr(session, "get", get)
    response = requests.Response()
    response.status_code = 200
    response.raw = BytesIO(b'{"result": {"raw": [1, 2]}, "retCode": 0}')
    responses.extend([response] * 16)
    path = tmp_path / "nested" / "archive.sqlite"
    if collect(path, session) != 1:
        pytest.fail("Expected collect(path, session) == 1")
    if collect(path, session) != 1:
        pytest.fail("Expected collect(path, session) == 1")
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute(
            "SELECT source, requested_at, available_at, payload, error "
            "FROM tunnel_inputs ORDER BY id"
        ).fetchall()
    expected_rows = 18
    if len(rows) != expected_rows:
        pytest.fail("Expected len(rows) == 18")
    if rows[0][3] != response.text:
        pytest.fail("Expected rows[0][3] == response.text")
    if not ((rows[0][1]) <= (rows[0][2])):
        pytest.fail("Expected rows[0][1] <= rows[0][2]")
    if rows[8][0] != "margin":
        pytest.fail("Expected rows[8][0] == 'margin'")
    if rows[8][4] != "ValueError":
        pytest.fail("Expected rows[8][4] == 'ValueError'")


def test_exchange_failure_keeps_body_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify exchange failure keeps body and continues."""
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    session = requests.Session()
    responses: list[requests.Response] = []

    def get(*_args: object, **_kwargs: object) -> requests.Response:
        """Get."""
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
    if collect(path, session) != 2:
        pytest.fail("Expected collect(path, session) == 2")
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute("SELECT payload, error FROM tunnel_inputs").fetchall()
    if rows[0] != (bad.text, "ValueError"):
        pytest.fail("Expected rows[0] == (bad.text, 'ValueError')")
    if rows[1] != (good.text, ""):
        pytest.fail("Expected rows[1] == (good.text, '')")


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
    if results != (0, 0):
        pytest.fail("Every configured source must be saved successfully")
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
    if legacy != [(None, None)]:
        pytest.fail("Migration must preserve legacy availability semantics")
    expected_passes = 2
    if len(groups) != expected_passes or any(
        row[2:] != (9, 9) for row in groups
    ):
        pytest.fail(
            "Each pass must retain nine separately identifiable sources"
        )
    if any(not str(row[1]).endswith(":00+00:00") for row in groups):
        pytest.fail("Collection labels must be UTC minute boundaries")
    if {row[0] for row in sources_saved} != {
        "instruments",
        "options",
        "spot",
        "depth",
        "candles",
        "ticker",
        "hourly_candles",
        "oi",
        "margin",
    }:
        pytest.fail("A required snapshot tunnel source is missing")
    if any(
        row[2] or '"preserved":[1,2,3]' not in row[1] for row in sources_saved
    ):
        pytest.fail("Archive must preserve successful payloads verbatim")
    if (
        "https://api.bybit.com/v5/market/kline",
        {
            "category": "linear",
            "symbol": "BTCUSDT",
            "interval": "1",
            "limit": "1000",
        },
    ) not in requested:
        pytest.fail("Price history must use native one-minute candles")
    if (
        "https://api.bybit.com/v5/market/tickers",
        {
            "category": "linear",
            "symbol": "BTCUSDT",
        },
    ) not in requested:
        pytest.fail("Current open interest must be sampled each collection")
