from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAPER_BOT_CONFIG_FILE = PROJECT_ROOT / "paper_bot_config.json"
SUPPORTED_TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1d"}
SUPPORTED_DATA_SOURCES = {"Bybit REST", "MEXC REST"}
BYBIT_TICKERS = {"BTC_USDT", "XRP_USDT", "DOGE_USDT", "ADA_USDT", "TRX_USDT", "LINK_USDT", "AVAX_USDT", "DOT_USDT", "TON_USDT", "NEAR_USDT"}
MEXC_TICKERS = {"BTC_USDT", "SHIB_USDT", "VET_USDT", "VED_USDT"}
SUPPORTED_TICKERS = BYBIT_TICKERS | MEXC_TICKERS


@dataclass(frozen=True)
class PaperBotConfig:
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
    open_sigma_1: float | None = None
    close_sigma_1: float | None = None
    open_sigma_2: float | None = None
    close_sigma_2: float | None = None

    def validate(self) -> None:
        if self.strategy_mode not in {"AVWAP crossover", "VWAP band mean reversion"}:
            raise ValueError("Unsupported paper bot strategy mode.")
        if self.timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError(f"Paper bot timeframe must be one of {sorted(SUPPORTED_TIMEFRAMES)}.")
        if not 0.1 <= self.open_order_vwap_sigma <= 3:
            raise ValueError("Open-order VWAP sigma must be between 0.1 and 3.")
        if not 0 <= self.close_order_vwap_sigma <= 3:
            raise ValueError("Close-order VWAP sigma must be between 0 and 3.")
        if self.initial_capital <= 0:
            raise ValueError("Initial paper capital must be positive.")
        if self.minimum_order_size < 0.01:
            raise ValueError("Minimum order size must be at least 0.01.")
        sigmas = (self.open_sigma_1, self.close_sigma_1, self.open_sigma_2, self.close_sigma_2)
        if any(value is not None for value in sigmas):
            if any(value is None for value in sigmas):
                raise ValueError("All four real-order sigma values must be configured together.")
            if not (0 < self.open_sigma_1 <= 3 and 0 < self.close_sigma_1 <= 3):
                raise ValueError("Sigma leg 1 must use positive values between 0 and 3.")
            if not (-3 <= self.open_sigma_2 < 0 and -3 <= self.close_sigma_2 < 0):
                raise ValueError("Sigma leg 2 must use negative values between -3 and 0.")
        if self.stop_loss_pct < 0:
            raise ValueError("Stop loss cannot be negative.")
        if not 1 <= self.vwap_anchor_reset_weeks <= 52:
            raise ValueError("VWAP anchor reset must be between 1 and 52 weeks.")
        if not isinstance(self.anchor_before_strategy_start, bool):
            raise ValueError("Anchor before strategy start must be true or false.")
        if not isinstance(self.anchor_before_days, int) or not 0 <= self.anchor_before_days <= 3650:
            raise ValueError("Anchor-before-strategy days must be between 0 and 3650.")
        if self.anchor_before_strategy_start and self.anchor_before_days < 1:
            raise ValueError("Anchor-before-strategy days must be at least 1 when enabled.")
        if self.data_source not in SUPPORTED_DATA_SOURCES:
            raise ValueError(f"Paper bot data source must be one of {sorted(SUPPORTED_DATA_SOURCES)}.")
        if self.ticker not in SUPPORTED_TICKERS:
            raise ValueError(f"Paper bot ticker must be one of {sorted(SUPPORTED_TICKERS)}.")
        if self.data_source == "Bybit REST" and self.ticker not in BYBIT_TICKERS:
            raise ValueError(f"The Bybit paper bot supports only {sorted(BYBIT_TICKERS)}.")
        if self.data_source == "MEXC REST" and self.ticker not in MEXC_TICKERS:
            raise ValueError(f"The MEXC paper bot supports only {sorted(MEXC_TICKERS)}.")
        if not isinstance(self.reverse_ticker, bool):
            raise ValueError("Reverse ticker must be true or false.")
        if self.open_position_side not in {"Long", "Short", "Both"}:
            raise ValueError("Open position side must be Long, Short, or Both.")

    def to_json(self) -> str:
        self.validate()
        return json.dumps({key: value for key, value in asdict(self).items() if value is not None}, indent=2) + "\n"

    def save(self, path: Path = PAPER_BOT_CONFIG_FILE) -> None:
        content = self.to_json()
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)

    @classmethod
    def load(cls, path: Path = PAPER_BOT_CONFIG_FILE) -> "PaperBotConfig":
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
            values.setdefault("anchor_before_strategy_start", False)
            values.setdefault("anchor_before_days", 0)
            values.setdefault("data_source", "Bybit REST")
            values.setdefault("ticker", "BTC_USDT")
            values.setdefault("reverse_ticker", False)
            values.pop("fee_pct", None)  # Removed: fees are read from the selected exchange.
            values.setdefault("open_position_side", "Both")
            values.setdefault("minimum_order_size", 0.01)
            values.setdefault("open_sigma_1", None)
            values.setdefault("close_sigma_1", None)
            values.setdefault("open_sigma_2", None)
            values.setdefault("close_sigma_2", None)
            config = cls(**values)
        except FileNotFoundError as exc:
            raise RuntimeError(f"Config file not found: {path}. Save it from the Streamlit app first.") from exc
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not load paper bot config {path}: {exc}") from exc
        config.validate()
        return config
