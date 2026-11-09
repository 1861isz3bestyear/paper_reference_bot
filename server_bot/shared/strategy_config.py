# Copyright 2026 anchored-vwam-backtest contributors
"""Read-only strategy settings shared by calculator and server."""

from typing import Protocol


class StrategyMarket(Protocol):
    """Market and anchor settings used to select strategy observations."""

    @property
    def timeframe(self) -> str:
        """Read the timeframe setting."""
        ...

    @property
    def vwap_anchor_reset_weeks(self) -> int:
        """Read the vwap anchor reset weeks setting."""
        ...

    @property
    def anchor_before_strategy_start(self) -> bool:
        """Read the anchor before strategy start setting."""
        ...

    @property
    def anchor_before_days(self) -> int:
        """Read the anchor before days setting."""
        ...

    @property
    def data_source(self) -> str:
        """Read the data source setting."""
        ...

    @property
    def ticker(self) -> str:
        """Read the ticker setting."""
        ...

    @property
    def reverse_ticker(self) -> bool:
        """Read the reverse ticker setting."""
        ...


class StrategyConfig(StrategyMarket, Protocol):
    """Strategy settings consumed by shared accounting and publishing."""

    @property
    def strategy_mode(self) -> str:
        """Read the strategy mode setting."""
        ...

    @property
    def trend(self) -> bool:
        """Read the trend setting."""
        ...

    @property
    def open_order_vwap_sigma(self) -> float:
        """Read the open order vwap sigma setting."""
        ...

    @property
    def close_order_vwap_sigma(self) -> float:
        """Read the close order vwap sigma setting."""
        ...

    @property
    def initial_capital(self) -> float:
        """Read the initial capital setting."""
        ...

    @property
    def stop_loss_pct(self) -> float:
        """Read the stop loss pct setting."""
        ...

    @property
    def allow_immediate_reentry(self) -> bool:
        """Read the allow immediate reentry setting."""
        ...

    @property
    def open_position_side(self) -> str:
        """Read the open position side setting."""
        ...

    @property
    def minimum_order_size(self) -> float:
        """Read the minimum order size setting."""
        ...

    @property
    def stop_loss_basis(self) -> str:
        """Read the stop loss basis setting."""
        ...

    @property
    def hedge_entry_timeout_bars(self) -> int:
        """Read the hedge entry timeout bars setting."""
        ...

    @property
    def leverage(self) -> int:
        """Read the leverage setting."""
        ...

    @property
    def slippage_bps(self) -> float:
        """Read the slippage bps setting."""
        ...

    @property
    def take_profit_pct(self) -> float:
        """Read the take profit pct setting."""
        ...

    def to_json(self) -> str:
        """Serialize all settings used for configuration identity."""
        ...
