import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from server_bot import cli


def test_packages_share_config_and_isolate_runtime_files() -> None:
    from live_paper_bot import cli as live_cli
    from live_paper_bot.config import PAPER_BOT_CONFIG_FILE as live_config
    from live_paper_bot.trading import PAPER_ACCOUNT_FILE as live_account
    from reference_bot import cli as reference_cli
    from reference_bot.config import PAPER_BOT_CONFIG_FILE as reference_config
    from reference_bot.trading import PAPER_ACCOUNT_FILE as reference_account

    assert reference_config == live_config
    assert reference_account != live_account
    assert reference_cli.STATE_FILE != live_cli.STATE_FILE
    assert reference_cli.CANDLE_CACHE_FILE != live_cli.CANDLE_CACHE_FILE
    assert reference_cli.PID_FILE != live_cli.PID_FILE
    assert reference_cli.INSTANCE_LOCK_FILE != live_cli.INSTANCE_LOCK_FILE


def test_start_both_uses_the_same_config(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "paper_bot_config.json"
    load = Mock()
    reference = Mock()
    live = Mock()
    monkeypatch.setattr(cli.PaperBotConfig, "load", load)
    monkeypatch.setattr(cli, "start_reference", reference)
    monkeypatch.setattr(cli, "start_live", live)

    cli.start_both(config_path, resume=True)

    load.assert_called_once_with(config_path)
    reference.assert_called_once_with(config_path, True)
    live.assert_called_once_with(config_path, True)


def test_reset_both_resets_each_runtime(monkeypatch) -> None:
    reference = Mock()
    live = Mock()
    monkeypatch.setattr(cli, "reset_reference", reference)
    monkeypatch.setattr(cli, "reset_live", live)

    cli.reset_both()

    reference.assert_called_once_with()
    live.assert_called_once_with()


def test_health_check_reports_reference_ten_candles_behind(monkeypatch, tmp_path: Path) -> None:
    reference_state = tmp_path / "reference.json"
    live_state = tmp_path / "live.json"
    reference_state.write_text(json.dumps({"latest_cached_candle": "2026-08-20T11:49:00+00:00"}))
    live_state.write_text(json.dumps({"latest_cached_candle": "2026-08-20T11:59:00+00:00"}))
    monkeypatch.setattr(cli, "REFERENCE_STATE_FILE", reference_state)
    monkeypatch.setattr(cli, "LIVE_STATE_FILE", live_state)
    monkeypatch.setattr(cli.PaperBotConfig, "load", Mock(return_value=Mock(timeframe="1m")))

    status, message = cli.health_check(
        tmp_path / "config.json", now=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    )

    assert status == cli.REFERENCE_STALE
    assert "10.0 candles behind" in message


def test_health_check_does_not_blame_reference_when_live_paper_is_stale(monkeypatch, tmp_path: Path) -> None:
    reference_state = tmp_path / "reference.json"
    live_state = tmp_path / "live.json"
    reference_state.write_text(json.dumps({"latest_cached_candle": "2026-08-20T11:30:00+00:00"}))
    live_state.write_text(json.dumps({"latest_cached_candle": "2026-08-20T11:50:00+00:00"}))
    monkeypatch.setattr(cli, "REFERENCE_STATE_FILE", reference_state)
    monkeypatch.setattr(cli, "LIVE_STATE_FILE", live_state)
    monkeypatch.setattr(cli.PaperBotConfig, "load", Mock(return_value=Mock(timeframe="1m")))

    status, message = cli.health_check(
        tmp_path / "config.json", now=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    )

    assert status == cli.INDETERMINATE
    assert "live-paper data" in message
