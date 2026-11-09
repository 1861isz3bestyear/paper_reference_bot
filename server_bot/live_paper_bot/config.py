# Copyright 2026 anchored-vwam-backtest contributors
"""Configuration validation and persistence for the paper server."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from shared.hedge_strategy import HEDGE_MODE
from shared.margin_types import validate_leverage
from shared.validation import (
    boolean,
    integer,
    json_object,
    number,
    object_mapping,
    string,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAPER_BOT_CONFIG_FILE = PROJECT_ROOT / "paper_bot_config.json"
SUPPORTED_TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1d"}
SUPPORTED_DATA_SOURCES = {"Bybit REST", "MEXC REST"}
BYBIT_TICKERS = {
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
MEXC_TICKERS = {"BTC_USDT", "SHIB_USDT", "VET_USDT", "VED_USDT"}
SUPPORTED_TICKERS = BYBIT_TICKERS | MEXC_TICKERS
MINIMUM_ENTRY_SIGMA = 0.1
MAXIMUM_SIGMA = 3
MINIMUM_ORDER_SIZE = 0.01
MAXIMUM_ANCHOR_WEEKS = 52
MAXIMUM_ANCHOR_DAYS = 3650


def _config_number(value: object) -> float:
    """Validate a finite number while retaining its JSON representation.

    Configuration fingerprints must match before and after a disk round trip,
    including when callers pass integers for float-valued fields.

    Returns:
        The original integer, or a validated float.

    """
    validated = number(value)
    return value if isinstance(value, int) else validated


def _optional_config_number(value: object) -> float | None:
    """Validate an optional numeric configuration field.

    Returns:
        A validated number, or None for an unset sigma.

    """
    return None if value is None else _config_number(value)


@dataclass(frozen=True, kw_only=True)
class PaperBotConfig:
    """Persist the server strategies and their exchange execution settings."""

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
    open_sigma_1: float | None = None
    close_sigma_1: float | None = None
    open_sigma_2: float | None = None
    close_sigma_2: float | None = None

    def validate(self) -> None:
        """Validate settings before starting or serializing a trading bot."""
        self._validate_strategy()
        self._validate_entry()
        self._validate_real_sigmas()
        self._validate_risk()
        self._validate_anchor()
        self._validate_market()

    def _validate_strategy(self) -> None:
        """Validate supported strategy modes and hedge constraints.

        Raises:
            ValueError: If the settings are unsupported or inconsistent.

        """
        validate_leverage(self.leverage, self.slippage_bps)
        if self.leverage > 1 and (
            self.data_source != "Bybit REST" or self.reverse_ticker
        ):
            message = "Leverage requires non-reversed Bybit USDT perpetuals."
            raise ValueError(message)
        if self.strategy_mode not in {
            "AVWAP crossover",
            "VWAP band mean reversion",
            HEDGE_MODE,
        }:
            message = "Unsupported paper bot strategy mode."
            raise ValueError(message)
        if self.timeframe not in SUPPORTED_TIMEFRAMES:
            message = (
                "Paper bot timeframe must be one of"
                f" {sorted(SUPPORTED_TIMEFRAMES)}."
            )
            raise ValueError(message)
        if self.strategy_mode == HEDGE_MODE:
            if (
                self.open_order_vwap_sigma != 0
                or (
                    not math.isfinite(self.close_order_vwap_sigma)
                    or self.close_order_vwap_sigma <= 0
                )
                or self.open_position_side != "Both"
            ):
                message = (
                    "VWAP zero hedge requires entry sigma 0, positive TP sigma,"
                    " and Both sides."
                )
                raise ValueError(message)
            if self.data_source != "Bybit REST" or self.reverse_ticker:
                message = (
                    "VWAP zero hedge requires non-reversed Bybit perpetuals."
                )
                raise ValueError(message)

    def _validate_entry(self) -> None:
        """Validate ordinary entry bands and allocation limits.

        Raises:
            ValueError: If the settings are unsupported or inconsistent.

        """
        if (
            self.strategy_mode != HEDGE_MODE
            and not MINIMUM_ENTRY_SIGMA
            <= self.open_order_vwap_sigma
            <= MAXIMUM_SIGMA
        ):
            message = "Open-order VWAP sigma must be between 0.1 and 3."
            raise ValueError(message)
        if (
            self.strategy_mode != HEDGE_MODE
            and not 0 <= self.close_order_vwap_sigma <= MAXIMUM_SIGMA
        ):
            message = "Close-order VWAP sigma must be between 0 and 3."
            raise ValueError(message)
        if self.initial_capital <= 0:
            message = "Initial paper capital must be positive."
            raise ValueError(message)
        if self.minimum_order_size < MINIMUM_ORDER_SIZE:
            message = "Minimum order size must be at least 0.01."
            raise ValueError(message)

    def _validate_real_sigmas(self) -> None:
        """Require a complete and correctly signed pair of sigma legs.

        Raises:
            ValueError: If the settings are unsupported or inconsistent.

        """
        sigmas = (
            self.open_sigma_1,
            self.close_sigma_1,
            self.open_sigma_2,
            self.close_sigma_2,
        )
        if any(value is not None for value in sigmas):
            if (
                self.open_sigma_1 is None
                or self.close_sigma_1 is None
                or self.open_sigma_2 is None
                or self.close_sigma_2 is None
            ):
                message = (
                    "All four real-order sigma values must be configured"
                    " together."
                )
                raise ValueError(message)
            if not (
                0 < self.open_sigma_1 <= MAXIMUM_SIGMA
                and 0 < self.close_sigma_1 <= MAXIMUM_SIGMA
            ):
                message = (
                    "Sigma leg 1 must use positive values between 0 and 3."
                )
                raise ValueError(message)
            if not (
                -MAXIMUM_SIGMA <= self.open_sigma_2 < 0
                and -MAXIMUM_SIGMA <= self.close_sigma_2 < 0
            ):
                message = (
                    "Sigma leg 2 must use negative values between -3 and 0."
                )
                raise ValueError(message)

    def _validate_risk(self) -> None:
        """Validate stop losses, take profit, and hedge entry timeout.

        Raises:
            ValueError: If the settings are unsupported or inconsistent.

        """
        if self.stop_loss_basis not in {"Position", "Deposit"}:
            message = "Stop loss basis must be Position or Deposit."
            raise ValueError(message)
        if (
            type(self.hedge_entry_timeout_bars) is not int
            or self.hedge_entry_timeout_bars < 1
        ):
            message = "Entry timeout must be at least one candle."
            raise ValueError(message)
        if not math.isfinite(self.take_profit_pct) or self.take_profit_pct < 0:
            message = "Take profit must be finite and nonnegative."
            raise ValueError(message)
        if self.stop_loss_pct < 0:
            message = "Stop loss cannot be negative."
            raise ValueError(message)

    def _validate_anchor(self) -> None:
        """Validate reset intervals and optional prelaunch history.

        Raises:
            ValueError: If the settings are unsupported or inconsistent.

        """
        if not 1 <= self.vwap_anchor_reset_weeks <= MAXIMUM_ANCHOR_WEEKS:
            message = "VWAP anchor reset must be between 1 and 52 weeks."
            raise ValueError(message)
        if type(self.anchor_before_strategy_start) is not bool:
            message = "Anchor before strategy start must be true or false."
            raise ValueError(message)
        if (
            type(self.anchor_before_days) is not int
            or not 0 <= self.anchor_before_days <= MAXIMUM_ANCHOR_DAYS
        ):
            message = "Anchor-before-strategy days must be between 0 and 3650."
            raise ValueError(message)
        if self.anchor_before_strategy_start and self.anchor_before_days < 1:
            message = (
                "Anchor-before-strategy days must be at least 1 when enabled."
            )
            raise ValueError(message)

    def _validate_market(self) -> None:
        """Validate supported exchanges, markets, and directions.

        Raises:
            ValueError: If the settings are unsupported or inconsistent.

        """
        if self.data_source not in SUPPORTED_DATA_SOURCES:
            message = (
                "Paper bot data source must be one of"
                f" {sorted(SUPPORTED_DATA_SOURCES)}."
            )
            raise ValueError(message)
        if self.ticker not in SUPPORTED_TICKERS:
            message = (
                f"Paper bot ticker must be one of {sorted(SUPPORTED_TICKERS)}."
            )
            raise ValueError(message)
        if (
            self.data_source == "Bybit REST"
            and self.ticker not in BYBIT_TICKERS
        ):
            message = (
                f"The Bybit paper bot supports only {sorted(BYBIT_TICKERS)}."
            )
            raise ValueError(message)
        if self.data_source == "MEXC REST" and self.ticker not in MEXC_TICKERS:
            message = (
                f"The MEXC paper bot supports only {sorted(MEXC_TICKERS)}."
            )
            raise ValueError(message)
        if type(self.reverse_ticker) is not bool:
            message = "Reverse ticker must be true or false."
            raise ValueError(message)
        if self.open_position_side not in {"Long", "Short", "Both"}:
            message = "Open position side must be Long, Short, or Both."
            raise ValueError(message)

    def to_json(self) -> str:
        """Serialize validated configuration without unset optional fields.

        Returns:
            Stable JSON used for persistence and configuration fingerprints.

        """
        self.validate()
        return (
            json.dumps(
                {
                    key: value
                    for key, value in object_mapping(asdict(self)).items()
                    if value is not None
                },
                indent=2,
            )
            + "\n"
        )

    def save(self, path: Path = PAPER_BOT_CONFIG_FILE) -> None:
        """Atomically replace the persisted configuration."""
        content = self.to_json()
        temporary = path.with_suffix(path.suffix + ".tmp")
        _ = temporary.write_text(content, encoding="utf-8")
        _ = temporary.replace(path)

    @classmethod
    def load(cls, path: Path = PAPER_BOT_CONFIG_FILE) -> PaperBotConfig:
        """Read and validate a current or legacy configuration document.

        Returns:
            The validated configuration.

        Raises:
            RuntimeError: If the document is missing or malformed.

        """
        try:
            values = json_object(path.read_text(encoding="utf-8"))
            config = cls._from_values(values)
        except FileNotFoundError as exc:
            message = (
                f"Config file not found: {path}. Save it from the Streamlit app"
                " first."
            )
            raise RuntimeError(message) from exc
        except (OSError, TypeError, ValueError, KeyError) as exc:
            message = f"Could not load paper bot config {path}: {exc}"
            raise RuntimeError(message) from exc
        config.validate()
        return config

    @classmethod
    def _from_values(cls, values: dict[str, object]) -> PaperBotConfig:
        """Validate JSON field types before constructing the configuration.

        Returns:
            A configuration with validated field types.

        Raises:
            TypeError: If unknown fields are present.

        """
        _ = values.pop("fee_pct", None)
        allowed = {field.name for field in fields(cls)}
        if values.keys() - allowed:
            message = "Unknown paper bot configuration fields."
            raise TypeError(message)
        return cls(
            strategy_mode=string(values["strategy_mode"]),
            timeframe=string(values["timeframe"]),
            trend=boolean(values["trend"]),
            open_order_vwap_sigma=_config_number(
                values["open_order_vwap_sigma"]
            ),
            close_order_vwap_sigma=_config_number(
                values["close_order_vwap_sigma"]
            ),
            initial_capital=_config_number(values["initial_capital"]),
            stop_loss_pct=_config_number(values["stop_loss_pct"]),
            allow_immediate_reentry=boolean(values["allow_immediate_reentry"]),
            vwap_anchor_reset_weeks=integer(values["vwap_anchor_reset_weeks"]),
            anchor_before_strategy_start=boolean(
                values.get("anchor_before_strategy_start", False)
            ),
            anchor_before_days=integer(values.get("anchor_before_days", 0)),
            data_source=string(values.get("data_source", "Bybit REST")),
            ticker=string(values.get("ticker", "BTC_USDT")),
            reverse_ticker=boolean(values.get("reverse_ticker", False)),
            open_position_side=string(values.get("open_position_side", "Both")),
            minimum_order_size=_config_number(
                values.get("minimum_order_size", 0.01)
            ),
            stop_loss_basis=string(values.get("stop_loss_basis", "Position")),
            hedge_entry_timeout_bars=integer(
                values.get("hedge_entry_timeout_bars", 3)
            ),
            leverage=integer(values.get("leverage", 1)),
            slippage_bps=_config_number(values.get("slippage_bps", 0.0)),
            take_profit_pct=_config_number(values.get("take_profit_pct", 0.0)),
            open_sigma_1=_optional_config_number(values.get("open_sigma_1")),
            close_sigma_1=_optional_config_number(values.get("close_sigma_1")),
            open_sigma_2=_optional_config_number(values.get("open_sigma_2")),
            close_sigma_2=_optional_config_number(values.get("close_sigma_2")),
        )
