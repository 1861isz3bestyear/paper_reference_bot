# Copyright 2026 anchored-vwam-backtest contributors
"""Reject unsupported deployed paper settings before a runner can start."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest

from live_paper_bot.config import PaperBotConfig as LiveConfig
from reference_bot.config import PaperBotConfig as ReferenceConfig


@pytest.fixture(params=[ReferenceConfig, LiveConfig])
def config(request: pytest.FixtureRequest) -> ReferenceConfig | LiveConfig:
    """Create a valid configuration for each independently deployed runner.

    Returns:
        The configured test value.

    Raises:
        TypeError: If a fixture contains an unsupported factory.

    """
    selected = cast("object", request.param)
    if selected is ReferenceConfig:
        factory = ReferenceConfig
    elif selected is LiveConfig:
        factory = LiveConfig
    else:
        message = "Unsupported configuration factory."
        raise TypeError(message)
    return factory(
        strategy_mode="VWAP band mean reversion",
        timeframe="1m",
        trend=True,
        open_order_vwap_sigma=1,
        close_order_vwap_sigma=2,
        initial_capital=1000,
        stop_loss_pct=1,
        allow_immediate_reentry=True,
        vwap_anchor_reset_weeks=1,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("open_order_vwap_sigma", 0),
        ("close_order_vwap_sigma", 4),
        ("initial_capital", 0),
        ("minimum_order_size", 0),
        ("stop_loss_basis", "unknown"),
        ("hedge_entry_timeout_bars", 0),
        ("take_profit_pct", -1),
        ("stop_loss_pct", -1),
        ("vwap_anchor_reset_weeks", 0),
        ("anchor_before_strategy_start", 1),
        ("anchor_before_days", -1),
        ("anchor_before_strategy_start", True),
        ("data_source", "unknown"),
        ("ticker", "SHIB_USDT"),
        ("reverse_ticker", 1),
        ("open_position_side", "unknown"),
    ],
)
def test_reject_invalid_config(
    config: ReferenceConfig | LiveConfig, field: str, value: object
) -> None:
    """Validate individual numerical, market and schedule constraints."""
    with pytest.raises(
        ValueError,
        match=r"must|cannot|positive|negative|supports|between|Initial|Minimum|Entry|Stop|Open|Reverse",
    ):
        replace(config, **{field: value}).validate()


def test_hedge_and_leverage_constraints(
    config: ReferenceConfig | LiveConfig,
) -> None:
    """Require two-sided non-reversed Bybit hedge and leverage settings."""
    hedge = replace(
        config, strategy_mode="VWAP zero hedge", open_order_vwap_sigma=0
    )
    hedge.validate()
    with pytest.raises(ValueError, match=r"requires"):
        replace(hedge, open_position_side="Long").validate()
    with pytest.raises(ValueError, match=r"requires"):
        replace(hedge, reverse_ticker=True).validate()
    with pytest.raises(ValueError, match=r"Leverage"):
        replace(config, leverage=2, reverse_ticker=True).validate()
    with pytest.raises(ValueError, match=r"MEXC"):
        replace(config, data_source="MEXC REST", ticker="XRP_USDT").validate()


@pytest.mark.parametrize(("long_sigma", "short_sigma"), [(-1, -1), (1, 1)])
def test_sigma_pair_signs(
    config: ReferenceConfig | LiveConfig, long_sigma: float, short_sigma: float
) -> None:
    """Reject reversed signs in either explicit order leg."""
    invalid = replace(
        config,
        open_sigma_1=long_sigma,
        close_sigma_1=2,
        open_sigma_2=short_sigma,
        close_sigma_2=-2,
    )
    with pytest.raises(ValueError, match=r"Sigma leg"):
        invalid.validate()
