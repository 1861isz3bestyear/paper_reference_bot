# Copyright 2026 anchored-vwam-backtest contributors
"""Tests for server bot test bybit tickers."""

from pathlib import Path

import pytest

from bybit_demo_bot.cli import BybitDemoBot, DemoState
from live_paper_bot.config import BYBIT_TICKERS as LIVE_TICKERS
from live_paper_bot.config import PaperBotConfig as LiveConfig
from reference_bot.config import BYBIT_TICKERS as REFERENCE_TICKERS
from reference_bot.config import PaperBotConfig as ReferenceConfig

EXPECTED = {
    "BTC_USDT",
    "XRP_USDT",
    "DOGE_USDT",
    "ADA_USDT",
    "TRX_USDT",
    "LINK_USDT",
    "AVAX_USDT",
    "DOT_USDT",
    "TON_USDT",
    "NEAR_USDT",
}


def values(ticker):
    """Values."""
    return {
        "strategy_mode": "VWAP band mean reversion",
        "timeframe": "1m",
        "trend": True,
        "open_order_vwap_sigma": 1,
        "close_order_vwap_sigma": 2,
        "initial_capital": 14,
        "stop_loss_pct": 0.4,
        "allow_immediate_reentry": True,
        "vwap_anchor_reset_weeks": 2,
        "data_source": "Bybit REST",
        "ticker": ticker,
    }


@pytest.mark.parametrize("ticker", sorted(EXPECTED))
def test_all_entities_accept_supported_bybit_ticker(
    ticker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify all entities accept supported bybit ticker."""
    if not (LIVE_TICKERS == REFERENCE_TICKERS == EXPECTED):
        pytest.fail("Expected LIVE_TICKERS == REFERENCE_TICKERS == EXPECTED")
    live, reference = LiveConfig(**values(ticker)), ReferenceConfig(
        **values(ticker)
    )
    live.validate()
    reference.validate()
    monkeypatch.setattr(
        "bybit_demo_bot.cli.STATE_FILE", tmp_path / "state.json"
    )
    demo = BybitDemoBot(
        reference, object(), DemoState("2026-01-01T00:00:00+00:00")
    )
    if demo.symbol != ticker.replace("_", ""):
        pytest.fail("Expected demo.symbol == ticker.replace('_', '')")


def test_bybit_only_ticker_is_not_accepted_for_mexc() -> None:
    """Verify bybit only ticker is not accepted for mexc."""
    config = ReferenceConfig(
        **{**values("XRP_USDT"), "data_source": "MEXC REST"}
    )
    with pytest.raises(ValueError, match="MEXC"):
        config.validate()
