# Copyright 2026 anchored-vwam-backtest contributors
"""Verify the streaming application and historical hedge accounting agree."""

import pandas as pd
import pytest

from reference_bot.cli import BotState
from reference_bot.config import PaperBotConfig
from shared.hedge_strategy import HEDGE_MODE, HedgeEngine, process_hedge_history
from shared.indicators import add_launch_weekly_anchored_vwap
from shared.strategy import backtest
from shared.test_hedge_strategy import row


def test_streaming_and_backtest_use_same_closed_trade_accounting() -> None:
    """Verify streaming hedge ledger and backtest use same closed trade.

    accounting.
    """
    cfg = PaperBotConfig(
        strategy_mode=HEDGE_MODE,
        timeframe="1m",
        trend=True,
        open_order_vwap_sigma=0.0,
        allow_immediate_reentry=False,
        initial_capital=1000,
        close_order_vwap_sigma=1.2,
        stop_loss_pct=1,
        stop_loss_basis="Position",
        hedge_entry_timeout_bars=3,
        minimum_order_size=0.01,
        anchor_before_strategy_start=False,
        anchor_before_days=0,
        vwap_anchor_reset_weeks=2,
        take_profit_pct=0.0,
    )
    state = BotState(launched_at="2026-01-01T00:00:00+00:00")
    raw = pd.DataFrame([
        vars(row(0, high=100, low=100)),
        vars(row(1, opening=101, high=102, low=100, close=102)),
        vars(row(2, opening=101, high=101, low=100)),
        vars(row(3, opening=100.5, high=101, low=100)),
        vars(row(4, opening=100, high=104, low=96)),
    ])
    raw["volume"] = 10
    for length in range(1, len(raw) + 1):
        _ = process_hedge_history(
            state,
            raw.iloc[:length],
            cfg,
            launched_at=pd.to_datetime(raw["time"], utc=True).iloc[0],
            fee_pct=0.1,
        )
    assert state.hedge_state is not None
    streaming = HedgeEngine(**state.hedge_state)
    streaming.close_all(
        raw["close"].astype(float).iloc[-1],
        pd.to_datetime(raw["time"], utc=True).iloc[-1],
        "End of strategy",
    )
    data = add_launch_weekly_anchored_vwap(
        raw, pd.to_datetime(raw["time"], utc=True).iloc[0], 2
    )
    eq, trades = backtest(
        data,
        pd.to_datetime(raw["time"], utc=True).iloc[0],
        strategy_end_at=pd.to_datetime(raw["time"], utc=True).iloc[-1],
        initial_capital=1000,
        fee_pct=0.1,
        stop_loss_pct=1,
        take_profit_pct=0,
        allow_reentry=True,
        strategy_mode=HEDGE_MODE,
        band_sigma=0,
        exit_band_sigma=1.2,
        minimum_order_size=0.01,
    )
    assert trades == streaming.trades()
    assert eq["equity"].astype(float).iloc[-1] == pytest.approx(streaming.cash)
    before = state.hedge_state
    assert (
        process_hedge_history(
            state,
            raw,
            cfg,
            launched_at=pd.to_datetime(raw["time"], utc=True).iloc[0],
            fee_pct=0.1,
        )
        == 0
    )
    assert state.hedge_state == before

