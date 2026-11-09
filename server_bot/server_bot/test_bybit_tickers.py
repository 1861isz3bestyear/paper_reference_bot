# Copyright 2026 anchored-vwam-backtest contributors
"""Tests for server bot test bybit tickers."""

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from bybit_demo_bot.cli import BybitDemoBot, DemoState
from bybit_demo_bot.client import BybitDemoClient
from live_paper_bot.config import BYBIT_TICKERS as LIVE_TICKERS
from live_paper_bot.config import PaperBotConfig as LiveConfig
from reference_bot.config import BYBIT_TICKERS as REFERENCE_TICKERS
from reference_bot.config import PaperBotConfig as ReferenceConfig

if TYPE_CHECKING:
    from pathlib import Path

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


def ticker_config(ticker: str) -> ReferenceConfig:
    """Build a server strategy for a supported perpetual market.

    Returns:
        The ticker-specific server configuration.

    """
    return ReferenceConfig(
        strategy_mode="VWAP band mean reversion",
        timeframe="1m",
        trend=True,
        open_order_vwap_sigma=1,
        close_order_vwap_sigma=2,
        initial_capital=14,
        stop_loss_pct=0.4,
        allow_immediate_reentry=True,
        vwap_anchor_reset_weeks=2,
        data_source="Bybit REST",
        ticker=ticker,
    )


@pytest.mark.parametrize("ticker", sorted(EXPECTED))
def test_all_entities_accept_supported_bybit_ticker(
    ticker: str, tmp_path: Path
) -> None:
    """Verify all entities accept supported bybit ticker."""
    assert LIVE_TICKERS == REFERENCE_TICKERS == EXPECTED
    reference = ticker_config(ticker)
    path = tmp_path / "config.json"
    reference.save(path)
    live = LiveConfig.load(path)
    live.validate()
    reference.validate()
    demo = BybitDemoBot(
        reference,
        BybitDemoClient("test-key", "test-secret"),
        DemoState("2026-01-01T00:00:00+00:00"),
    )
    assert demo.symbol == ticker.replace("_", "")


def test_bybit_only_ticker_is_not_accepted_for_mexc() -> None:
    """Verify bybit only ticker is not accepted for mexc."""
    config = replace(ticker_config("XRP_USDT"), data_source="MEXC REST")
    with pytest.raises(ValueError, match="MEXC"):
        config.validate()
