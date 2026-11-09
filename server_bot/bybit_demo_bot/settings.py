# Copyright 2026 anchored-vwam-backtest contributors
"""Paths and candle intervals shared by exchange executors."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTERVAL_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}
