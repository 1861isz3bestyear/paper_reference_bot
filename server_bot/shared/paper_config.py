# Copyright 2026 anchored-vwam-backtest contributors
"""Provide config behavior."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from shared.hedge_types import HEDGE_MODE
from shared.margin_types import validate_leverage
from shared.tunnel import TunnelSettings
from shared.tunnel_config import (
    TUNNEL_MODE,
    parse_tunnel,
    tunnel_values,
)
from shared.validation import boolean, integer, json_object, number, string

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAPER_BOT_CONFIG_FILE = PROJECT_ROOT / "paper_bot_config.json"
SERVER_BOT_CONFIG_FILE = PROJECT_ROOT / "server_bot_config.json"
SERVER_CONFIG_DIRECTORY = (
    PROJECT_ROOT.parent / "paper" / "server_bot" / "server_bot" / "configs"
)
SUPPORTED_TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1d"}
SUPPORTED_DATA_SOURCES = {"Bybit REST", "MEXC REST"}
SUPPORTED_TICKERS = {"BTC_USDT", "SHIB_USDT", "VET_USDT", "VED_USDT"}
SERVER_BYBIT_TICKERS = {
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


def _validate_config_fields(
    values: dict[str, object], allowed: set[str]
) -> None:
    """Reject configuration keys that this client cannot interpret.

    Raises:
        TypeError: If an unsupported configuration field is present.

    """
    if values.keys() - allowed:
        msg = "Unknown paper bot configuration fields."
        raise TypeError(msg)


MINIMUM_SIGMA = 0.1
MAXIMUM_SIGMA = 3.0
MINIMUM_ORDER_SIZE = 0.01
MAXIMUM_ANCHOR_WEEKS = 52
MAXIMUM_ANCHOR_DAYS = 3650


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_boolean(value: object) -> bool:
    return isinstance(value, bool)


@dataclass(frozen=True, kw_only=True)
class PaperBotConfig:
    """Represent paperbotconfig."""

    strategy_mode: str
    timeframe: str
    trend: bool
    open_order_vwap_sigma: float
    close_order_vwap_sigma: float
    initial_capital: float
    stop_loss_pct: float
    allow_immediate_reentry: bool
    vwap_anchor_reset_weeks: int
    anchor_before_strategy_start: bool = False
    anchor_before_days: int = 0
    data_source: str = "Bybit REST"
    ticker: str = "BTC_USDT"
    reverse_ticker: bool = False
    open_position_side: str = "Both"
    minimum_order_size: float = 0.01
    stop_loss_basis: str = "Position"
    hedge_entry_timeout_bars: int = 3
    leverage: int = 1
    slippage_bps: float = 0.0
    take_profit_pct: float = 0.0
    tunnel: TunnelSettings = field(default_factory=TunnelSettings)

    def validate(self, *, for_server: bool = False) -> None:
        """Handle validate."""
        self._validate_strategy()
        self._validate_risk()
        self._validate_anchor()
        self._validate_market(for_server=for_server)

    def _validate_strategy(self) -> None:
        """Validate strategy selection and sigma settings.

        Raises:
            ValueError: If a strategy setting is invalid.

        """
        validate_leverage(self.leverage, self.slippage_bps)
        if self.leverage > 1 and (
            self.data_source != "Bybit REST" or self.reverse_ticker
        ):
            msg = "Leverage requires non-reversed Bybit USDT perpetuals."
            raise ValueError(msg)
        if self.strategy_mode not in {
            "AVWAP crossover",
            "VWAP band mean reversion",
            HEDGE_MODE,
            TUNNEL_MODE,
        }:
            msg = "Unsupported paper bot strategy mode."
            raise ValueError(msg)
        if self.timeframe not in SUPPORTED_TIMEFRAMES:
            msg = (
                "Paper bot timeframe must be one of"
                f" {sorted(SUPPORTED_TIMEFRAMES)}."
            )
            raise ValueError(msg)
        if self.strategy_mode == HEDGE_MODE:
            if (
                self.open_order_vwap_sigma != 0
                or (
                    not math.isfinite(self.close_order_vwap_sigma)
                    or self.close_order_vwap_sigma <= 0
                )
                or self.open_position_side != "Both"
            ):
                msg = (
                    "VWAP zero hedge requires entry sigma 0, positive TP sigma,"
                    " and Both sides."
                )
                raise ValueError(msg)
            if self.data_source != "Bybit REST" or self.reverse_ticker:
                msg = "VWAP zero hedge requires non-reversed Bybit perpetuals."
                raise ValueError(msg)
        if self.strategy_mode == TUNNEL_MODE:
            self._validate_tunnel()
        self._validate_non_hedge_sigma()

    def _validate_tunnel(self) -> None:
        """Require BTC perpetual prices and explicit protective exits.

        Raises:
            ValueError: If the selected market or risk mode is incompatible.

        """
        if (
            self.data_source != "Bybit REST"
            or self.ticker != "BTC_USDT"
            or self.reverse_ticker
            or self.leverage != 1
        ):
            msg = "Snapshot tunnel requires non-reversed Bybit BTC_USDT at 1x."
            raise ValueError(msg)
        if not (
            math.isfinite(self.stop_loss_pct)
            and self.stop_loss_pct > 0
            and math.isfinite(self.take_profit_pct)
            and self.take_profit_pct > 0
        ):
            msg = "Snapshot tunnel requires positive stop loss and take profit."
            raise ValueError(msg)

    def _validate_non_hedge_sigma(self) -> None:
        """Validate sigma settings for non-hedge strategies.

        Raises:
            ValueError: If a sigma setting is invalid.

        """
        if self.strategy_mode in {HEDGE_MODE, TUNNEL_MODE}:
            return
        if not MINIMUM_SIGMA <= self.open_order_vwap_sigma <= MAXIMUM_SIGMA:
            msg = "Open-order VWAP sigma must be between 0.1 and 3."
            raise ValueError(msg)
        if not -MAXIMUM_SIGMA <= self.close_order_vwap_sigma <= MAXIMUM_SIGMA:
            msg = "Close-order VWAP sigma must be between -3 and 3."
            raise ValueError(msg)
        if (
            self.close_order_vwap_sigma < 0
            and self.strategy_mode != "VWAP band mean reversion"
        ):
            msg = (
                "Negative close-order VWAP sigma is only valid for mean"
                " reversion."
            )
            raise ValueError(msg)

    def _validate_risk(self) -> None:
        """Validate account risk settings.

        Raises:
            ValueError: If a risk setting is invalid.

        """
        if self.stop_loss_basis not in {"Position", "Deposit"}:
            msg = "Stop loss basis must be Position or Deposit."
            raise ValueError(msg)
        if (
            not _is_integer(self.hedge_entry_timeout_bars)
            or self.hedge_entry_timeout_bars < 1
        ):
            msg = "Entry timeout must be at least one candle."
            raise ValueError(msg)
        if self.initial_capital <= 0:
            msg = "Initial paper capital must be positive."
            raise ValueError(msg)
        if self.minimum_order_size < MINIMUM_ORDER_SIZE:
            msg = "Minimum order size must be at least 0.01."
            raise ValueError(msg)
        if self.stop_loss_pct < 0:
            msg = "Stop loss cannot be negative."
            raise ValueError(msg)
        if not math.isfinite(self.take_profit_pct) or self.take_profit_pct < 0:
            msg = "Take profit cannot be negative or non-finite."
            raise ValueError(msg)

    def _validate_anchor(self) -> None:
        """Validate anchored-VWAP timing settings.

        Raises:
            ValueError: If an anchor setting is invalid.

        """
        if not 1 <= self.vwap_anchor_reset_weeks <= MAXIMUM_ANCHOR_WEEKS:
            msg = "VWAP anchor reset must be between 1 and 52 weeks."
            raise ValueError(msg)
        if not _is_boolean(self.anchor_before_strategy_start):
            msg = "Anchor before strategy start must be true or false."
            raise ValueError(msg)
        if (
            not _is_integer(self.anchor_before_days)
            or not 0 <= self.anchor_before_days <= MAXIMUM_ANCHOR_DAYS
        ):
            msg = "Anchor-before-strategy days must be between 0 and 3650."
            raise ValueError(msg)
        if self.anchor_before_strategy_start and self.anchor_before_days < 1:
            msg = "Anchor-before-strategy days must be at least 1 when enabled."
            raise ValueError(msg)

    def _validate_market(self, *, for_server: bool) -> None:
        """Validate market and data-source settings.

        Raises:
            ValueError: If a market setting is invalid.

        """
        if self.data_source not in SUPPORTED_DATA_SOURCES:
            msg = (
                "Paper bot data source must be one of"
                f" {sorted(SUPPORTED_DATA_SOURCES)}."
            )
            raise ValueError(msg)
        supported_tickers = (
            SERVER_BYBIT_TICKERS
            if for_server and self.data_source == "Bybit REST"
            else SUPPORTED_TICKERS
        )
        if self.ticker not in supported_tickers:
            msg = (
                f"Paper bot ticker must be one of {sorted(supported_tickers)}."
            )
            raise ValueError(msg)
        if (
            not for_server
            and self.data_source == "Bybit REST"
            and self.ticker != "BTC_USDT"
        ):
            msg = "The Bybit paper bot currently supports only BTC_USDT."
            raise ValueError(msg)
        if not _is_boolean(self.reverse_ticker):
            msg = "Reverse ticker must be true or false."
            raise ValueError(msg)
        if self.open_position_side not in {"Long", "Short", "Both"}:
            msg = "Open position side must be Long, Short, or Both."
            raise ValueError(msg)

    def to_json(self) -> str:
        """Handle to json.

        Returns:
            The resulting value.

        Raises:
            ValueError: If the operation cannot be completed.

        """
        self.validate()
        if self.leverage > 1:
            msg = "Use the Lima server export for leveraged paper trading."
            raise ValueError(msg)
        values: dict[str, object] = asdict(self)
        if self.strategy_mode == TUNNEL_MODE:
            values["tunnel"] = tunnel_values(self.tunnel)
        else:
            _ = values.pop("tunnel")
        return json.dumps(values, indent=2) + "\n"

    def to_server_json(self) -> str:
        """Export the extended configuration consumed by lima/server_bot.

        Returns:
            The computed result.

        Raises:
            ValueError: If the strategy has no Lima implementation.

        """
        if self.strategy_mode == TUNNEL_MODE:
            msg = "Snapshot tunnel runs with the local paper bot, not Lima."
            raise ValueError(msg)
        self.validate(for_server=True)
        values: dict[str, object] = asdict(self)
        _ = values.pop("tunnel")
        # Zero-sigma exits are valid for the reference strategy, but the
        # optional
        # signed-leg schema requires strictly positive/negative close levels.
        if (
            self.strategy_mode != HEDGE_MODE
            and self.close_order_vwap_sigma != 0
        ):
            values.update(
                open_sigma_1=self.open_order_vwap_sigma,
                close_sigma_1=self.close_order_vwap_sigma,
                open_sigma_2=-self.open_order_vwap_sigma,
                close_sigma_2=-self.close_order_vwap_sigma,
            )
        return json.dumps(values, indent=2) + "\n"

    def save(self, path: Path = PAPER_BOT_CONFIG_FILE) -> None:
        """Handle save."""
        content = self.to_json()
        temporary = path.with_suffix(path.suffix + ".tmp")
        _ = temporary.write_text(content, encoding="utf-8")
        _ = temporary.replace(path)

    def save_server(
        self, name: str, directory: Path = SERVER_CONFIG_DIRECTORY
    ) -> Path:
        """Save a named Lima config without overwriting an existing preset.

        Returns:
            The computed result.

        Raises:
            ValueError: If the operation cannot be completed.

        """
        name = name.strip()
        name = name.removesuffix(".json")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", name):
            msg = (
                "Use 1\N{EN DASH}80 letters, numbers, underscores or hyphens,"
                " starting with a letter or number."
            )
            raise ValueError(msg)
        content = self.to_server_json()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.json"
        with path.open("x", encoding="utf-8") as destination:
            _ = destination.write(content)
        return path

    @classmethod
    def load(cls, path: Path = PAPER_BOT_CONFIG_FILE) -> PaperBotConfig:
        """Handle load.

        Returns:
            The resulting value.

        Raises:
            RuntimeError: If the operation cannot be completed.
            ValueError: If the operation cannot be completed.

        """
        try:
            values = json_object(path.read_text(encoding="utf-8"))
            allowed = {
                "strategy_mode",
                "timeframe",
                "trend",
                "open_order_vwap_sigma",
                "close_order_vwap_sigma",
                "initial_capital",
                "stop_loss_pct",
                "allow_immediate_reentry",
                "vwap_anchor_reset_weeks",
                "anchor_before_strategy_start",
                "anchor_before_days",
                "data_source",
                "ticker",
                "reverse_ticker",
                "open_position_side",
                "minimum_order_size",
                "stop_loss_basis",
                "hedge_entry_timeout_bars",
                "leverage",
                "slippage_bps",
                "take_profit_pct",
                "fee_pct",
                "tunnel",
            }
            _validate_config_fields(values, allowed)
            config = cls(
                strategy_mode=string(values["strategy_mode"]),
                timeframe=string(values["timeframe"]),
                trend=boolean(values["trend"]),
                open_order_vwap_sigma=number(values["open_order_vwap_sigma"]),
                close_order_vwap_sigma=number(values["close_order_vwap_sigma"]),
                initial_capital=number(values["initial_capital"]),
                stop_loss_pct=number(values["stop_loss_pct"]),
                allow_immediate_reentry=boolean(
                    values["allow_immediate_reentry"]
                ),
                vwap_anchor_reset_weeks=integer(
                    values["vwap_anchor_reset_weeks"]
                ),
                anchor_before_strategy_start=boolean(
                    values.get("anchor_before_strategy_start", False)
                ),
                anchor_before_days=integer(values.get("anchor_before_days", 0)),
                data_source=string(values.get("data_source", "Bybit REST")),
                ticker=string(values.get("ticker", "BTC_USDT")),
                reverse_ticker=boolean(values.get("reverse_ticker", False)),
                open_position_side=string(
                    values.get("open_position_side", "Both")
                ),
                minimum_order_size=number(
                    values.get("minimum_order_size", 0.01)
                ),
                stop_loss_basis=string(
                    values.get("stop_loss_basis", "Position")
                ),
                hedge_entry_timeout_bars=integer(
                    values.get("hedge_entry_timeout_bars", 3)
                ),
                leverage=integer(values.get("leverage", 1)),
                slippage_bps=number(values.get("slippage_bps", 0.0)),
                take_profit_pct=number(values.get("take_profit_pct", 0.0)),
                tunnel=parse_tunnel(values.get("tunnel", {})),
            )
        except FileNotFoundError as exc:
            msg = (
                f"Config file not found: {path}. Save it from the Streamlit app"
                " first."
            )
            raise RuntimeError(msg) from exc
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            msg = f"Could not load paper bot config {path}: {exc}"
            raise RuntimeError(msg) from exc
        config.validate()
        if config.leverage > 1:
            msg = "Run leveraged configs with the Lima server bots in paper/."
            raise ValueError(msg)
        return config
