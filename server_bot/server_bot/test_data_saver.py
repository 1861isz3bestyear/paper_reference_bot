# Copyright 2026 anchored-vwam-backtest contributors
"""Exercise standalone persistence, restart, validation and process behavior."""

from __future__ import annotations

import json
import runpy
import sys
from io import BytesIO
from typing import TYPE_CHECKING

import pytest
import requests

from reference_bot.market import CandleCache
from server_bot import data_saver

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    "setting",
    [
        {"interval": "2m"},
        {"symbol": ""},
        {"symbol": "БТК"},
        {"symbol": "BTC/USDT"},
        {"poll_seconds": 0},
        {"poll_seconds": float("nan")},
        {"history_days": 0},
    ],
)
def test_invalid_settings(setting: dict[str, str | float | int]) -> None:
    """Reject invalid configuration before creating files."""
    with pytest.raises(ValueError, match=r".+"):
        _ = data_saver.SaverConfig(
            interval=str(setting.get("interval", "1m")),
            symbol=str(setting.get("symbol", "BTCUSDT")),
            poll_seconds=float(setting.get("poll_seconds", 30)),
            history_days=int(setting.get("history_days", 7)),
        )


def test_persistence_restart_and_completed_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persist real paginated API responses and resume without duplicates."""
    config = data_saver.SaverConfig(database=tmp_path / "nested/candles.sqlite")
    expected_count = 2
    last_start = 60000
    timeout_seconds = 20
    starts: list[int] = []
    pages: list[list[list[str]]] = [
        [
            [str(t), "10", "12", "9", "11", "2", "22"]
            for t in (120000, 60000, 0)
        ],
        [],
        [["60000", "10", "12", "9", "11", "2", "22"]],
    ]

    def get(
        _url: str, *, params: dict[str, str | int], timeout: int
    ) -> requests.Response:
        """Return exchange pages through the real response decoder.

        Returns:
            Public HTTP response fixture.

        """
        if timeout != timeout_seconds or params["interval"] != "1":
            pytest.fail("Expected public 1-minute request with timeout")
        starts.append(int(params["start"]))
        response = requests.Response()
        response.status_code = 200
        response.raw = BytesIO(
            json.dumps(
                {"retCode": 0, "result": {"list": pages.pop(0)}}
            ).encode()
        )
        return response

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(data_saver, "time_ns", lambda: 120000 * 1_000_000)
    if data_saver.collect(config) != expected_count:
        pytest.fail("Expected only two completed candles")
    if data_saver.collect(config, now_ms=120000) != 1:
        pytest.fail("Expected overlap refresh")
    rows = CandleCache(config.database).load("BTCUSDT", "1m", 0, 180000)
    if len(rows) != expected_count or starts[-1] != last_start:
        pytest.fail(
            "Restart must resume latest stored candle without duplicates"
        )
    pages.append([])
    if data_saver.collect(config, now_ms=120000) != 0:
        pytest.fail("Empty response should write no candles")


@pytest.mark.parametrize("failure", [False, True])
def test_once_exit_status(
    monkeypatch: pytest.MonkeyPatch, *, failure: bool
) -> None:
    """Report failed single collections to shell callers."""

    def collect(_config: data_saver.SaverConfig) -> int:
        """Simulate the collection boundary.

        Returns:
            Saved count.

        Raises:
            requests.ConnectionError: For the failure scenario.

        """
        if failure:
            raise requests.ConnectionError
        return 1

    monkeypatch.setattr(data_saver, "collect", collect)
    if data_saver.main(["--once"]) != int(failure):
        pytest.fail("Incorrect single-run status")


def test_retry_and_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry network failures and exit cleanly on interruption."""
    expected_calls = 2
    calls: list[int] = []

    def collect(_config: data_saver.SaverConfig) -> int:
        """Fail first collection and complete the retry.

        Returns:
            Saved count.

        Raises:
            requests.Timeout: On first request.

        """
        calls.append(1)
        if len(calls) == 1:
            raise requests.Timeout
        return 0

    def sleep(_seconds: float) -> None:
        """Interrupt after the successful retry.

        Raises:
            KeyboardInterrupt: After retry.

        """
        if len(calls) == expected_calls:
            raise KeyboardInterrupt

    monkeypatch.setattr(data_saver, "collect", collect)
    monkeypatch.setattr(data_saver, "sleep", sleep)
    if data_saver.main([]) != 0 or len(calls) != expected_calls:
        pytest.fail("Expected retry then graceful shutdown")


def test_cli_invalid_config() -> None:
    """Expose invalid configuration as an argparse error."""
    with pytest.raises(SystemExit, match="2"):
        _ = data_saver.main(["--poll-seconds", "-1"])


def test_module_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify python -m exposes help without collecting data."""
    monkeypatch.setattr("sys.argv", ["data_saver", "--help"])
    monkeypatch.delitem(sys.modules, "server_bot.data_saver")
    with pytest.raises(SystemExit, match="0"):
        _ = runpy.run_module("server_bot.data_saver", run_name="__main__")
