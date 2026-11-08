# Copyright 2026 anchored-vwam-backtest contributors
"""Complete history must be accepted regardless of pandas datetime resolution."""

from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest


@pytest.mark.parametrize("package", ["reference_bot", "live_paper_bot"])
@pytest.mark.parametrize("unit", ["ns", "us", "ms"])
@pytest.mark.parametrize("missing_middle", [False, True])
def test_history_gap_check_uses_milliseconds_not_storage_units(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    package,
    unit,
    missing_middle,
) -> None:
    """Verify history gap check uses milliseconds not storage units."""
    module = import_module(package + ".cli")
    helpers = import_module(package + ".test_paper_bot")
    bot = helpers.TestPaperBot().make_bot(str(tmp_path))
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
    # Preserve the chosen resolution at the cache boundary, independent of pandas version.
    bot.candle_cache = Mock()
    bot.candle_cache.load.return_value = history
    monkeypatch.setattr(
        module, "fetch_completed_linear_klines", Mock(return_value=history)
    )
    if missing_middle:
        with pytest.raises(RuntimeError, match="incomplete"):
            bot._load_completed_candles(
                times[0], datetime(2026, 1, 1, 0, 16, tzinfo=UTC)
            )
    else:
        loaded = bot._load_completed_candles(
            times[0], datetime(2026, 1, 1, 0, 16, tzinfo=UTC)
        )
        if len(loaded) != 3:
            pytest.fail("Expected len(loaded) == 3")
        if not ((bot.state.last_successful_market_sync) is not (None)):
            pytest.fail(
                "Expected bot.state.last_successful_market_sync is not None"
            )
