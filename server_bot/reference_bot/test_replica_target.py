# Copyright 2026 anchored-vwam-backtest contributors
"""Tests for reference bot test replica target."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bybit_demo_bot.test_bybit_demo import config
from reference_bot.cli import PaperBot
from reference_bot.trading import LocalPosition
from shared.reference_target import config_fingerprint, publish_target


def test_target_contains_actual_ledger_position_and_identity(
    tmp_path: Path,
) -> None:
    """Verify target contains actual ledger position and identity."""
    cfg = config(ticker="XRP_USDT")
    state = SimpleNamespace(
        launched_at="2026-09-13T10:58:55+00:00",
        last_processed_candle="2026-09-13T11:00:00+00:00",
    )
    position = LocalPosition(
        "XRPUSDT", "Short", Decimal("7.4"), 1.34, "2026-09-13T11:00:04+00:00"
    )
    path = tmp_path / "reference_target.json"
    publish_target(path, cfg, state, position=position)
    data = json.loads(path.read_text())
    if data["quantity"] != "7.4":
        pytest.fail("Expected data['quantity'] == '7.4'")
    if data["entry_price"] != 1.34:
        pytest.fail("Expected data['entry_price'] == 1.34")
    if data["position_id"] != position.updated_at:
        pytest.fail("Expected data['position_id'] == position.updated_at")
    if data["config_fingerprint"] != config_fingerprint(cfg):
        pytest.fail(
            "Expected data['config_fingerprint'] == config_fingerprint(cfg)"
        )
    if not (
        (datetime.fromisoformat(data["published_at"]).tzinfo) is not (None)
    ):
        pytest.fail(
            "Expected datetime.fromisoformat(data['published_at']).tzinfo is"
            " not None"
        )
    if path.with_suffix(".json.tmp").exists():
        pytest.fail("Expected not path.with_suffix('.json.tmp').exists()")
    publish_target(
        path,
        cfg,
        state,
        position=LocalPosition("XRPUSDT", None, Decimal(0), None, None),
    )
    if (json.loads(path.read_text())["position_id"]) is not (None):
        pytest.fail(
            "Expected json.loads(path.read_text())['position_id'] is None"
        )


@pytest.mark.parametrize("fails", [False, True])
def test_runner_only_publishes_after_successful_reconciliation(
    tmp_path: Path, fails
) -> None:
    """Verify runner only publishes after successful reconciliation."""
    bot = PaperBot.__new__(PaperBot)
    bot.config = config(ticker="XRP_USDT")
    bot.state = SimpleNamespace(
        launched_at=datetime.now(UTC).isoformat(), last_processed_candle=None
    )
    bot.state_path = tmp_path / "reference_state.json"
    bot.quote_currency = "USDT"
    bot.trade_symbol = "XRPUSDT"
    bot.market_feed = Mock()
    bot.trader = Mock()
    bot.trader.current_position.return_value = LocalPosition(
        "XRPUSDT", None, Decimal(0), None, None
    )
    bot.running = True
    bot.process_available_candles = Mock(
        side_effect=RuntimeError("history incomplete") if fails else None
    )
    bot._wait = lambda _: setattr(bot, "running", False)
    bot.run_forever()
    if not (((tmp_path / "reference_target.json").exists()) is not (fails)):
        pytest.fail(
            "Expected (tmp_path / 'reference_target.json').exists() is not"
            " fails"
        )
    bot.market_feed.stop.assert_called_once()
