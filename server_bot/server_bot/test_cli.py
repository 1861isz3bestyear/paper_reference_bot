# Copyright 2026 anchored-vwam-backtest contributors
"""Tests for server bot test cli."""

import json
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from live_paper_bot import cli as live_cli
from live_paper_bot.config import PAPER_BOT_CONFIG_FILE as LIVE_CONFIG
from live_paper_bot.trading import PAPER_ACCOUNT_FILE as LIVE_ACCOUNT
from reference_bot import cli as reference_cli
from reference_bot.config import PAPER_BOT_CONFIG_FILE as REFERENCE_CONFIG
from reference_bot.config import PaperBotConfig
from reference_bot.trading import PAPER_ACCOUNT_FILE as REFERENCE_ACCOUNT
from server_bot import cli

if TYPE_CHECKING:
    from pathlib import Path


def test_packages_share_config_and_isolate_runtime_files() -> None:
    """Verify packages share config and isolate runtime files."""
    assert REFERENCE_CONFIG == LIVE_CONFIG
    assert REFERENCE_ACCOUNT != LIVE_ACCOUNT
    assert reference_cli.STATE_FILE != live_cli.STATE_FILE
    assert reference_cli.CANDLE_CACHE_FILE != live_cli.CANDLE_CACHE_FILE
    assert reference_cli.PID_FILE != live_cli.PID_FILE
    assert reference_cli.INSTANCE_LOCK_FILE != live_cli.INSTANCE_LOCK_FILE


def test_start_both_uses_the_same_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify start both uses the same config."""
    config_path = tmp_path / "paper_bot_config.json"
    load = Mock()
    reference = Mock()
    live = Mock()
    monkeypatch.setattr(PaperBotConfig, "load", load)
    monkeypatch.setattr(cli, "start_reference", reference)
    monkeypatch.setattr(cli, "start_live", live)

    cli.start_both(config_path, resume=True)

    load.assert_called_once_with(config_path)
    reference.assert_called_once_with(config_path, resume=True)
    live.assert_called_once_with(config_path, resume=True)


def test_reset_both_resets_each_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify reset both resets each runtime."""
    reference = Mock()
    live = Mock()
    monkeypatch.setattr(cli, "reset_reference", reference)
    monkeypatch.setattr(cli, "reset_live", live)

    cli.reset_both()

    reference.assert_called_once_with()
    live.assert_called_once_with()


def test_health_check_reports_reference_ten_candles_behind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify health check reports reference ten candles behind."""
    reference_state = tmp_path / "reference.json"
    live_state = tmp_path / "live.json"
    _ = reference_state.write_text(
        json.dumps({"latest_cached_candle": "2026-08-20T11:49:00+00:00"})
    )
    _ = live_state.write_text(
        json.dumps({"latest_cached_candle": "2026-08-20T11:59:00+00:00"})
    )
    monkeypatch.setattr(cli, "REFERENCE_STATE_FILE", reference_state)
    monkeypatch.setattr(cli, "LIVE_STATE_FILE", live_state)
    monkeypatch.setattr(
        PaperBotConfig, "load", Mock(return_value=Mock(timeframe="1m"))
    )

    status, message = cli.health_check(
        tmp_path / "config.json",
        now=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
    )

    assert status == cli.REFERENCE_STALE
    assert "10.0 candles behind" in message


def test_health_check_does_not_blame_reference_when_live_paper_is_stale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify health check does not blame reference when live paper is stale."""
    reference_state = tmp_path / "reference.json"
    live_state = tmp_path / "live.json"
    _ = reference_state.write_text(
        json.dumps({"latest_cached_candle": "2026-08-20T11:30:00+00:00"})
    )
    _ = live_state.write_text(
        json.dumps({"latest_cached_candle": "2026-08-20T11:50:00+00:00"})
    )
    monkeypatch.setattr(cli, "REFERENCE_STATE_FILE", reference_state)
    monkeypatch.setattr(cli, "LIVE_STATE_FILE", live_state)
    monkeypatch.setattr(
        PaperBotConfig, "load", Mock(return_value=Mock(timeframe="1m"))
    )

    status, message = cli.health_check(
        tmp_path / "config.json",
        now=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
    )

    assert status == cli.INDETERMINATE
    assert "live-paper data" in message


def test_configs_lists_only_sorted_json_exports(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify configs lists only sorted json exports."""
    for name in ("z.json", "a.json", "notes.txt"):
        _ = (tmp_path / name).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["server_bot", "configs", "--directory", str(tmp_path)]
    )
    cli.main()
    assert capsys.readouterr().out.splitlines() == [
        str(tmp_path / "a.json"),
        str(tmp_path / "z.json"),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        "null",
        '{"latest_cached_candle": 1}',
        '{"latest_cached_candle": "2026-08-20T11:59:00"}',
        "{invalid",
    ],
)
def test_health_check_handles_invalid_live_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: str
) -> None:
    """Malformed persisted state produces an indeterminate health result."""
    live_state = tmp_path / "live.json"
    _ = live_state.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(cli, "LIVE_STATE_FILE", live_state)
    monkeypatch.setattr(cli, "REFERENCE_STATE_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(
        PaperBotConfig, "load", Mock(return_value=Mock(timeframe="1m"))
    )
    status, message = cli.health_check(
        tmp_path / "config.json", now=datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    )
    assert status == cli.INDETERMINATE
    assert "no valid latest cached candle" in message


@pytest.mark.parametrize("future_peer", ["live", "reference"])
def test_health_check_rejects_future_candles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, future_peer: str
) -> None:
    """A future timestamp cannot establish a healthy pair of bots."""
    for peer, attribute in (
        ("live", "LIVE_STATE_FILE"),
        ("reference", "REFERENCE_STATE_FILE"),
    ):
        path = tmp_path / f"{peer}.json"
        timestamp = (
            "2026-08-20T12:01:00+00:00"
            if peer == future_peer
            else "2026-08-20T11:59:00+00:00"
        )
        _ = path.write_text(
            json.dumps({"latest_cached_candle": timestamp}), encoding="utf-8"
        )
        monkeypatch.setattr(cli, attribute, path)
    monkeypatch.setattr(
        PaperBotConfig, "load", Mock(return_value=Mock(timeframe="1m"))
    )
    status, message = cli.health_check(
        tmp_path / "config.json", now=datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    )
    assert status == (
        cli.INDETERMINATE if future_peer == "live" else cli.REFERENCE_STALE
    )
    assert "future" in message
