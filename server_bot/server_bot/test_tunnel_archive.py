# Copyright 2026 anchored-vwam-backtest contributors
"""Verify durable raw tunnel evidence and failure isolation."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from io import BytesIO
from pathlib import Path

import pytest
import requests

from server_bot.tunnel_archive import collect


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
    responses.extend([response] * 14)
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
    if len(rows) != 16:
        pytest.fail("Expected len(rows) == 16")
    if rows[0][3] != response.text:
        pytest.fail("Expected rows[0][3] == response.text")
    if not ((rows[0][1]) <= (rows[0][2])):
        pytest.fail("Expected rows[0][1] <= rows[0][2]")
    if rows[7][0] != "margin":
        pytest.fail("Expected rows[7][0] == 'margin'")
    if rows[7][4] != "ValueError":
        pytest.fail("Expected rows[7][4] == 'ValueError'")


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
    responses.extend([bad, good, good, good, good, good, good])
    path = tmp_path / "archive.sqlite"
    if collect(path, session) != 2:
        pytest.fail("Expected collect(path, session) == 2")
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute("SELECT payload, error FROM tunnel_inputs").fetchall()
    if rows[0] != (bad.text, "ValueError"):
        pytest.fail("Expected rows[0] == (bad.text, 'ValueError')")
    if rows[1] != (good.text, ""):
        pytest.fail("Expected rows[1] == (good.text, '')")
