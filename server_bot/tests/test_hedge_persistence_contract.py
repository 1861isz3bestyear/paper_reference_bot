# Copyright 2026 anchored-vwam-backtest contributors
"""Round-trip every nested hedge ledger record through runtime validation."""

from __future__ import annotations

import pytest

from shared import hedge_validation
from shared.hedge_strategy import HedgeEngine
from shared.test_hedge_strategy import engine, row


def test_nested_hedge_snapshot_validation() -> None:
    """Restore pending plans, filled legs and completed history without loss."""
    bot = engine()
    _ = bot.process(row(0), 100, 1)
    pending = bot.snapshot()
    if hedge_validation.parse_hedge_snapshot(pending) != pending:
        pytest.fail(
            "Expected hedge_validation.parse_hedge_snapshot(pending) == pending"
        )
    _ = bot.process(row(1), 100, 1)
    filled = bot.snapshot()
    if hedge_validation.parse_hedge_snapshot(filled) != filled:
        pytest.fail(
            "Expected hedge_validation.parse_hedge_snapshot(filled) == filled"
        )
    bot.close_all(101, row(2).time, "End of strategy")
    closed = bot.snapshot()
    if hedge_validation.parse_hedge_snapshot(closed) != closed:
        pytest.fail(
            "Expected hedge_validation.parse_hedge_snapshot(closed) == closed"
        )
    if hedge_validation.optional_str(None) is not None:
        pytest.fail("Expected hedge_validation.optional_str(None) is None")
    if hedge_validation.optional_float(None) is not None:
        pytest.fail("Expected hedge_validation.optional_float(None) is None")
    if hedge_validation.optional_str("timestamp") != "timestamp":
        pytest.fail(
            "Expected hedge_validation.optional_str('timestamp') == 'timestamp'"
        )
    if hedge_validation.optional_float(1) != 1:
        pytest.fail("Expected hedge_validation.optional_float(1) == 1")
    restored = HedgeEngine(**hedge_validation.parse_hedge_snapshot(closed))
    if restored.snapshot() != closed:
        pytest.fail("Expected restored.snapshot() == closed")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cash", float("nan")),
        ("cash", -1),
        ("stop_loss_basis", "invalid"),
        ("entry_timeout_bars", 0),
    ],
)
def test_invalid_persisted_hedge_parameters(field: str, value: object) -> None:
    """Refuse invalid persisted engine settings during restart."""
    snapshot = engine().snapshot()
    with pytest.raises(
        ValueError, match=r"finite|Invalid hedge|Stop loss basis|Entry timeout"
    ):
        _ = HedgeEngine(
            **hedge_validation.parse_hedge_snapshot(
                dict(snapshot, **{field: value})
            )
        )
