from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

@dataclass(frozen=True)
class Trade:
    deposit_size: float
    side: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    return_pct: float
    exit_reason: str
