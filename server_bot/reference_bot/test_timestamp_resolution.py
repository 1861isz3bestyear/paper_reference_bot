# Copyright 2026 anchored-vwam-backtest contributors
"""Accept complete history at every supported datetime resolution."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal
from unittest.mock import Mock

import pandas as pd
import pytest

from live_paper_bot import cli as live_cli
from live_paper_bot.test_paper_bot import TestPaperBot as LiveTestPaperBot
from reference_bot import cli as reference_cli
from reference_bot.test_paper_bot import TestPaperBot as ReferenceTestPaperBot
from tests.mock_support import mock_child

if TYPE_CHECKING:
    from pathlib import Path


CANDLE_COUNT = 3


@pytest.mark.parametrize("package", ["reference_bot", "live_paper_bot"])
@pytest.mark.parametrize("unit", ["ns", "us", "ms"])
@pytest.mark.parametrize("missing_middle", [False, True])
def test_history_gap_check_uses_milliseconds_not_storage_units(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    package: str,
    unit: Literal["ns", "us", "ms"],
    missing_middle: bool,
) -> None:
    """Verify history gap check uses milliseconds not storage units."""
    module = reference_cli if package == "reference_bot" else live_cli
    bot = (
        ReferenceTestPaperBot()
        if package == "reference_bot"
        else LiveTestPaperBot()
    ).make_bot(str(tmp_path))
    times = pd.date_range(
        "2026-01-01", periods=3, freq="5min", tz="UTC"
    ).as_unit(unit)
    history = pd.DataFrame({
        "time": times,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.0,
        "volume": 10.0,
        "quote_asset_volume": 1000.0,
        "number_of_trades": 1,
    })
    if missing_middle:
        history = history.iloc[[0, 2]]
    # Preserve the chosen resolution at the cache boundary, independent of
    # pandas version.
    bot.candle_cache = Mock()
    mock_child(bot.candle_cache, "load").return_value = history
    monkeypatch.setattr(
        module, "fetch_completed_linear_klines", Mock(return_value=history)
    )
    if missing_middle:
        with pytest.raises(RuntimeError, match="incomplete"):
            _ = bot.load_completed_candles(
                times[0], datetime(2026, 1, 1, 0, 16, tzinfo=UTC)
            )
    else:
        loaded = bot.load_completed_candles(
            times[0], datetime(2026, 1, 1, 0, 16, tzinfo=UTC)
        )
        assert len(loaded) == CANDLE_COUNT
        assert bot.state.last_successful_market_sync is not None
