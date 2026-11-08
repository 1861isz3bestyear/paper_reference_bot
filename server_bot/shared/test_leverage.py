# Copyright 2026 anchored-vwam-backtest contributors
"""Test leverage."""

import itertools
from datetime import date
from typing import TYPE_CHECKING, Unpack, cast, final

import pandas as pd
import pytest
import requests

from shared import leverage_data as data
from shared.candles import Candle
from shared.hedge_strategy import HEDGE_MODE
from shared.leverage import (
    MarginEngine as BaseMarginEngine,
)
from shared.leverage import (
    OpenOptions,
    leveraged_backtest,
)
from shared.leverage_data import (
    load_leverage_data,
    parse_risk_tiers,
    validate_contract,
)
from shared.margin_types import (
    Contract,
    MarginOrder,
    MarginSettings,
    RiskTier,
    validate_leverage,
)
from shared.strategy import backtest
from shared.testing_support import ignoring_arguments
from shared.validation import integer, numeric_string

if TYPE_CHECKING:
    from shared.testing_scenarios import MarginChanges


@final
class ErrorResponse:
    """Provide the errorresponse test double."""

    @staticmethod
    def raise_for_status() -> None:
        """Handle raise for status."""
        return

    @staticmethod
    def json() -> object:
        """Handle json.

        Returns:
            The resulting value.

        """
        return {"retCode": 10001, "retMsg": "bad symbol"}


class MarginEngine(BaseMarginEngine):
    """Provide the marginengine test double."""

    # ruff: ignore[too-many-arguments]
    def open_position(
        self,
        side: str,
        price: float,
        timestamp: pd.Timestamp,
        *,
        allocation: float,
        target: float | None = None,
        limit: bool = False,
    ) -> bool:
        """Open a position through the shared test engine.

        Returns:
            Whether the position was opened.

        """
        return self._open(
            side,
            price,
            timestamp,
            options=OpenOptions(
                allocation=allocation, target=target, limit=limit
            ),
        )

    def close_position(
        self,
        side: str,
        price: float,
        timestamp: pd.Timestamp,
        *,
        reason: str,
        limit: bool = False,
    ) -> None:
        """Close a position through the shared test engine."""
        self._close(side, price, timestamp, reason=reason, limit=limit)


def contract() -> Contract:
    """Provide the contract test fixture.

    Returns:
        The constructed test value.

    """
    return Contract(
        symbol="BTCUSDT",
        fetched_at="2026-01-01T00:00:00+00:00",
        max_leverage=100.0,
        leverage_step=1.0,
        quantity_step=0.001,
        min_quantity=0.001,
        min_notional=5.0,
        max_quantity=10000.0,
        price_tick=0.01,
        funding_interval_minutes=480,
        tiers=[
            RiskTier(
                limit=1000000.0, mmr=0.005, deduction=0.0, max_leverage=100.0
            )
        ],
    )


def settings(**changes: Unpack[MarginChanges]) -> MarginSettings:
    """Provide the settings test fixture.

    Returns:
        The constructed test value.

    """
    result = MarginSettings(
        config_json="",
        leverage=10,
        slippage_bps=0.0,
        fee_pct=0.1,
        strategy_mode="VWAP band mean reversion",
        band_sigma=1.0,
        exit_band_sigma=0.0,
        trend_mode=False,
        open_position_side="Long",
        stop_loss_pct=0.0,
        stop_loss_basis="Position",
        minimum_order_size=5.0,
        take_profit_pct=0.0,
        allow_reentry=True,
        hedge_entry_timeout_bars=3,
    )
    result.update(changes)
    return result


def candle(minute: int = 0, **changes: float) -> Candle:
    """Provide the candle test fixture.

    Returns:
        The constructed test value.

    """
    return Candle(
        time=pd.Timestamp("2026-01-01T00:00Z") + pd.Timedelta(minutes=minute),
        open=changes.get("open", 100.0),
        high=changes.get("high", 100.2),
        low=changes.get("low", 99.8),
        close=changes.get("close", 100.0),
        mark_open=changes.get("mark_open", 100.0),
        mark_high=changes.get("mark_high", 100.2),
        mark_low=changes.get("mark_low", 99.8),
        mark_close=changes.get("mark_close", 100.0),
        funding_rate=changes.get("funding_rate", 0.0),
        anchored_vwap=changes.get("anchored_vwap", 100.0),
        order_vwap=changes.get("order_vwap", 100.0),
        order_std=changes.get("order_std", 1.0),
    )


@pytest.mark.parametrize(("side", "direction"), [("Long", 1), ("Short", -1)])
def test_margin_sizing_fees_funding_and_net_pnl(
    side: str, direction: int
) -> None:
    """Verify margin sizing fees funding and net pnl."""
    engine = MarginEngine(1000.0, settings(), contract())
    assert engine.open_position(side, 100.0, candle().time, allocation=1000.0)
    p = engine.positions[side]
    q = p["quantity"]
    assert q == pytest.approx(98.039)
    assert p["margin"] == pytest.approx(q * 100 / 10)
    assert engine.equity(100.0) == pytest.approx(1000 - q * 100 * 0.001)
    _ = engine.process(
        candle(1, funding_rate=0.001), float("nan"), float("nan")
    )
    assert engine.positions[side]["funding_paid"] == pytest.approx(
        direction * q * 100 * 0.001
    )
    price = 100 + direction
    engine.close_position(side, price, candle(2).time, reason="Test")
    expected = (
        q - q * 100 * 0.001 - q * price * 0.001 - direction * q * 100 * 0.001
    )
    assert engine.cash == pytest.approx(1000 + expected)
    trade = engine.trades()[0]
    assert trade.pnl == pytest.approx(expected)
    assert trade.entry_notional == pytest.approx(q * 100)
    expected_value = 10
    assert trade.leverage == expected_value


@pytest.mark.parametrize(
    ("side", "marks"),
    [("Long", {"mark_low": 80.0}), ("Short", {"mark_high": 120.0})],
)
def test_liquidation_uses_mark_price_and_caps_isolated_loss(
    side: str, marks: dict[str, float]
) -> None:
    """Verify liquidation uses mark price and caps isolated loss."""
    engine = MarginEngine(1000.0, settings(stop_loss_pct=0.5), contract())
    _ = engine.open_position(side, 100.0, candle().time, allocation=400.0)
    remaining = engine.cash
    _ = engine.process(candle(1, **marks), 100.0, 1.0, allow_new=False)
    assert not engine.positions
    assert engine.cash == pytest.approx(remaining)
    assert engine.history[0]["exit_reason"] == "Liquidation"
    assert engine.history[0]["return_pct"] == pytest.approx(-100)


def test_funding_changes_liquidation_and_is_not_replayed_on_resume() -> None:
    """Verify funding changes liquidation and is not replayed on resume."""
    engine = MarginEngine(1000.0, settings(), contract())
    _ = engine.open_position("Long", 100.0, candle().time, allocation=500.0)
    before = engine.liquidation_price("Long", engine.positions["Long"])
    _ = engine.process(
        candle(1, funding_rate=0.005), float("nan"), float("nan")
    )
    after = engine.liquidation_price("Long", engine.positions["Long"])
    assert after > before
    restored = MarginEngine(**engine.snapshot())
    assert not restored.process(candle(1, funding_rate=0.005), 100.0, 1.0)
    assert restored.snapshot() == engine.snapshot()


def test_slippage_rounding_and_risk_tier_limits() -> None:
    """Verify slippage rounding and risk tier limits."""
    engine = MarginEngine(1000.0, settings(slippage_bps=10.0), contract())
    _ = engine.open_position("Long", 100.0, candle().time, allocation=1000.0)
    assert engine.positions["Long"]["entry_price"] == pytest.approx(100.1)
    engine.close_position("Long", 100.0, candle(1).time, reason="Stop loss")
    assert engine.history[0]["exit_price"] == pytest.approx(99.9)
    expected_value = 1000.0
    assert engine.cash < expected_value
    risk = contract()
    risk["tiers"] = [
        RiskTier(limit=1000.0, mmr=0.005, deduction=0.0, max_leverage=100.0),
        RiskTier(limit=100000.0, mmr=0.01, deduction=5.0, max_leverage=5.0),
    ]
    engine = MarginEngine(1000.0, settings(), risk)
    with pytest.raises(ValueError, match="risk-tier"):
        _ = engine.open_position(
            "Long", 100.0, candle().time, allocation=1000.0
        )
    limited = contract()
    limited["max_leverage"] = 50.0
    with pytest.raises(ValueError, match="not supported"):
        _ = MarginEngine(1000.0, settings(leverage=100), limited)
    engine = MarginEngine(0.01, settings(), contract())
    assert not engine.open_position(
        "Long", 100.0, candle().time, allocation=0.01
    )


@pytest.mark.parametrize("leverage", [0, 101, 2.5, True, float("nan")])
def test_rejects_invalid_leverage(leverage: object) -> None:
    """Verify rejects invalid leverage."""
    with pytest.raises(ValueError, match="Leverage"):
        validate_leverage(leverage, 0)


@pytest.mark.parametrize("slippage", [-1, 1001, float("nan"), float("inf")])
def test_rejects_invalid_slippage(slippage: float) -> None:
    """Verify rejects invalid slippage."""
    with pytest.raises(ValueError, match="Slippage"):
        validate_leverage(2, slippage)


def test_hedge_arms_two_legs_and_no_funding_before_fill() -> None:
    """Verify hedge arms two legs and no funding before fill."""
    engine = MarginEngine(
        1000.0,
        settings(strategy_mode=HEDGE_MODE, exit_band_sigma=1.2),
        contract(),
    )
    _ = engine.process(candle(), 100.0, 2.0)
    assert set(engine.pending) == {"Long", "Short"}
    assert not engine.positions
    _ = engine.process(candle(1, funding_rate=0.01), 100.0, 2.0)
    assert set(engine.positions) == {"Long", "Short"}
    assert all(p["funding_paid"] == 0 for p in engine.positions.values())
    expected_value = 1000
    assert sum(p["margin"] for p in engine.positions.values()) < expected_value
    _ = engine.process(candle(2, high=103.0, mark_high=103.0), 100.0, 2.0)
    assert "Long" not in engine.positions
    assert "Short" in engine.positions
    engine.close_all(100.0, candle(3).time, "End of strategy")
    expected_count = 2
    assert len(engine.trades()) == expected_count


@pytest.mark.parametrize(
    ("side", "entry_candle", "exit_candle", "expected_entry", "expected_exit"),
    [
        (
            "Short",
            {"open": 100.0, "high": 101.0, "low": 100.0, "close": 101.0},
            {"open": 100.5, "high": 100.5, "low": 99.3, "close": 99.3},
            101.0,
            99.3,
        ),
        (
            "Long",
            {"open": 100.0, "high": 100.0, "low": 99.0, "close": 99.0},
            {"open": 99.5, "high": 100.7, "low": 99.5, "close": 100.7},
            99.0,
            100.7,
        ),
    ],
)
def test_mean_reversion_negative_close_sigma_crosses_vwap(
    side: str,
    entry_candle: dict[str, float],
    exit_candle: dict[str, float],
    *,
    expected_entry: float,
    expected_exit: float,
) -> None:
    """Verify mean reversion negative close sigma crosses vwap."""
    engine = MarginEngine(
        1000.0,
        settings(
            leverage=2,
            fee_pct=0.0,
            exit_band_sigma=-0.7,
            open_position_side=side,
        ),
        contract(),
    )

    _ = engine.process(candle(0, **entry_candle), 100.0, 1.0)
    _ = engine.process(candle(1, **exit_candle), 100.0, 1.0)

    trade = engine.trades()[0]
    assert trade.side == side
    assert trade.entry_price == expected_entry
    assert trade.exit_price == pytest.approx(expected_exit)
    assert trade.exit_reason == "Reached exit band"


@pytest.mark.parametrize(
    "mode", ["AVWAP crossover", "VWAP band mean reversion", HEDGE_MODE]
)
def test_leveraged_backtest_closes_all_positions(mode: str) -> None:
    """Verify leveraged backtest closes all positions."""
    rows = [
        candle(
            i, high=103.0, low=97.0, mark_high=103.0, mark_low=97.0, close=101.0
        )
        for i in range(4)
    ]
    data = pd.DataFrame([vars(row) for row in rows])
    equity, trades = leveraged_backtest(
        data,
        1000.0,
        settings(strategy_mode=mode, exit_band_sigma=2.0),
        contract=contract(),
    )
    expected_count = 4
    assert len(equity) == expected_count
    assert not cast("object", equity.iloc[-1]["position"])
    assert trades
    assert numeric_string(
        cast("object", equity.iloc[-1]["equity"])
    ) == pytest.approx(1000 + sum(t.pnl for t in trades))


def test_loads_realistic_bybit_contract_marks_and_funding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify loads realistic bybit contract marks and funding."""
    timestamp = int(candle().time.timestamp() * 1000)
    calls: list[tuple[str, dict[str, str | int]]] = []

    def rows(endpoint: str, **params: str | int) -> dict[str, object]:
        calls.append((endpoint, params))
        if endpoint == "instruments-info":
            return {
                "list": [{
                    "contractType": "LinearPerpetual",
                    "settleCoin": "USDT",
                    "fundingInterval": 480,
                    "leverageFilter": {
                        "maxLeverage": "100",
                        "leverageStep": "1",
                    },
                    "priceFilter": {"tickSize": ".01"},
                    "lotSizeFilter": {
                        "qtyStep": ".001",
                        "minOrderQty": ".001",
                        "minNotionalValue": "5",
                        "maxMktOrderQty": "1000",
                    },
                }]
            }
        if endpoint == "risk-limit":
            return {
                "list": [{
                    "riskLimitValue": "1000000",
                    "maintenanceMargin": ".005",
                    "mmDeduction": "",
                    "maxLeverage": "100",
                }]
            }
        if endpoint == "mark-price-kline":
            return {
                "list": [
                    [str(timestamp + i * 60000), "100", "101", "99", "100"]
                    for i in range(2)
                ]
            }
        if endpoint == "funding/history":
            times = [timestamp - 480 * 60000, timestamp]
            return {
                "list": [
                    {"fundingRateTimestamp": str(t), "fundingRate": ".0001"}
                    for t in times
                    if integer(params["startTime"])
                    <= t
                    <= integer(params["endTime"])
                ]
            }
        raise AssertionError(endpoint)

    monkeypatch.setattr(data, "request_rows", rows)
    candles = pd.DataFrame([vars(candle(i)) for i in range(2)]).drop(
        columns=[
            "mark_open",
            "mark_high",
            "mark_low",
            "mark_close",
            "funding_rate",
        ]
    )
    enriched, info = load_leverage_data(candles, "BTCUSDT", "1m")
    assert enriched.funding_rate.tolist() == [0.0001, 0.0]
    assert enriched["mark_close"].astype(float).tolist() == [100.0, 100.0]
    assert info["quantity_step"] == pytest.approx(0.001)
    assert isinstance(info["funding_interval_minutes"], int)
    assert info["tiers"][0]["deduction"] == pytest.approx(0.0)
    assert {c[0] for c in calls} == {
        "instruments-info",
        "risk-limit",
        "mark-price-kline",
        "funding/history",
    }


@pytest.mark.parametrize("issue", ["marks", "funding", "interior", "gaps"])
def test_missing_inputs_fail_closed(
    monkeypatch: pytest.MonkeyPatch, issue: str
) -> None:
    """Verify missing inputs fail closed."""
    candles = pd.DataFrame([vars(candle(i)) for i in range(2)]).drop(
        columns=[
            "mark_open",
            "mark_high",
            "mark_low",
            "mark_close",
            "funding_rate",
        ]
    )
    marks = pd.DataFrame([vars(candle(i)) for i in range(2)])[
        ["time", "mark_open", "mark_high", "mark_low", "mark_close"]
    ]
    if issue == "marks":
        marks = marks.iloc[:1]
    if issue == "gaps":
        candle_time = cast("object", candles.loc[1, "time"])
        assert isinstance(candle_time, pd.Timestamp)
        candles.loc[1, "time"] = candle_time + pd.Timedelta(minutes=1)
    monkeypatch.setattr(
        data, "fetch_mark_prices", ignoring_arguments(lambda: marks)
    )
    if issue == "funding":
        monkeypatch.setattr(
            data,
            "request_rows",
            ignoring_arguments(lambda: {"list": list[object]()}),
        )
    else:
        monkeypatch.setattr(
            data,
            "fetch_funding",
            ignoring_arguments(
                lambda: (
                    [(candle().time + pd.Timedelta(seconds=30), 0.001)]
                    if issue == "interior"
                    else []
                )
            ),
        )
    with pytest.raises(ValueError, match=r"Funding|funding|mark|gap"):
        _ = load_leverage_data(candles, "BTCUSDT", "1m", contract=contract())


def test_missing_contract_or_mark_data_rejected_by_strategy() -> None:
    """Verify missing contract or mark data rejected by strategy."""
    data = pd.DataFrame(
        [dict(vars(candle(i)), anchored_std=1.0) for i in range(3)]
    )
    with pytest.raises(ValueError, match="requires contract"):
        _ = backtest(
            data,
            pd.to_datetime(data["time"], utc=True).iloc[0],
            strategy_end_at=pd.to_datetime(data["time"], utc=True).iloc[-1],
            initial_capital=1000,
            fee_pct=0.1,
            stop_loss_pct=0,
            take_profit_pct=0,
            allow_reentry=True,
            strategy_mode="AVWAP crossover",
            band_sigma=1,
            leverage=2,
        )


@pytest.mark.parametrize("leverage", [2, 100])
def test_leverage_endpoints_produce_correct_initial_margin(
    leverage: int,
) -> None:
    """Verify leverage endpoints produce correct initial margin."""
    engine = MarginEngine(1000.0, settings(leverage=leverage), contract())
    assert engine.open_position("Long", 100.0, candle().time, allocation=1000.0)
    position = engine.positions["Long"]
    assert position["quantity"] * 100 / position["margin"] == pytest.approx(
        leverage
    )
    expected_value = 100
    assert 0 < position["liquidation_price"] < expected_value


@pytest.mark.parametrize("side", ["Long", "Short"])
def test_gap_stop_charges_slippage_and_preserves_net_accounting(
    side: str,
) -> None:
    """Verify gap stop charges slippage and preserves net accounting."""
    engine = MarginEngine(
        1000.0,
        settings(leverage=2, stop_loss_pct=1, slippage_bps=2),
        contract(),
    )
    _ = engine.open_position(
        side, 100.0, candle().time, allocation=1000.0, limit=True
    )
    row = candle(
        1, open=98.0, low=97.0, high=103.0, mark_low=97.0, mark_high=103.0
    )
    _ = engine.process(row, float("nan"), float("nan"), allow_new=False)
    trade = engine.history[0]
    assert trade["exit_reason"] == "Stop loss"
    assert trade["exit_price"] == pytest.approx(
        98 * 0.9998 if side == "Long" else 101 * 1.0002
    )
    assert engine.cash == pytest.approx(1000 + trade["pnl"])
    assert trade["pnl"] == pytest.approx(
        trade["gross_pnl"]
        - trade["entry_fee"]
        - trade["exit_fee"]
        - trade["funding_paid"]
        + trade["liquidation_adjustment"]
    )


def test_take_profit_and_scheduled_session_closure() -> None:
    """Verify take profit and scheduled session closure."""
    engine = MarginEngine(
        1000.0, settings(leverage=2, take_profit_pct=1), contract()
    )
    _ = engine.open_position("Long", 100.0, candle().time, allocation=1000.0)
    _ = engine.process(
        candle(1, high=102.0, mark_high=102.0), float("nan"), float("nan")
    )
    assert engine.history[0]["exit_reason"] == "Take profit"
    data = pd.DataFrame([
        vars(candle(i, close=101.0, high=102.0, mark_high=102.0))
        for i in range(3)
    ])
    scheduled_days: pd.Series[date] = pd.Series(
        [date(2026, 1, 1), date(2026, 1, 1), date(2026, 1, 2)], dtype=object
    )
    _, trades = leveraged_backtest(
        data,
        1000.0,
        settings(strategy_mode="AVWAP crossover", leverage=2),
        contract=contract(),
        scheduled_days=scheduled_days,
    )
    assert trades[0].exit_reason == "End of scheduled session"


@pytest.mark.parametrize(
    ("mark_low", "low", "reason"),
    [
        (40.0, 98.0, "Liquidation"),
        (98.0, 98.0, "Stop loss"),
        (100.0, 100.0, "Take profit"),
    ],
)
def test_exit_precedence_settles_funding_and_blocks_reentry(
    mark_low: float, low: float, reason: str
) -> None:
    """Settle funding before competing exits and forbid same-bar reentry."""
    engine = MarginEngine(
        1000.0,
        settings(leverage=2, stop_loss_pct=1, take_profit_pct=1),
        contract(),
    )
    assert engine.open_position("Long", 100.0, candle().time, allocation=500.0)
    quantity = engine.positions["Long"]["quantity"]
    engine.pending["Short"] = MarginOrder(
        price=101.0, allocation=100.0, target=99.0
    )

    assert engine.process(
        candle(
            1,
            low=low,
            high=103.0,
            mark_low=mark_low,
            mark_high=103.0,
            funding_rate=0.001,
        ),
        101.0,
        1.0,
    )

    assert len(engine.history) == 1
    assert engine.history[0]["exit_reason"] == reason
    assert engine.history[0]["funding_paid"] == pytest.approx(
        quantity * 100.0 * 0.001
    )
    assert not engine.positions
    assert not engine.pending
    assert engine.reentry_ready


def test_duplicate_and_older_candles_leave_account_unchanged() -> None:
    """Ignore replayed candles before settling funding or changing signals."""
    engine = MarginEngine(1000.0, settings(leverage=2), contract())
    assert engine.open_position("Long", 100.0, candle().time, allocation=500.0)
    assert engine.process(
        candle(1, funding_rate=0.001), float("nan"), 1.0, allow_new=False
    )
    snapshot = engine.snapshot()

    for minute in (1, 0):
        assert not engine.process(
            candle(minute, funding_rate=0.1, mark_low=1.0), 100.0, 2.0
        )
        assert engine.snapshot() == snapshot


@pytest.mark.parametrize("trend_mode", [False, True])
def test_band_entry_keeps_short_priority_when_both_bands_are_reached(
    *, trend_mode: bool
) -> None:
    """Attempt the short first regardless of band direction."""
    engine = MarginEngine(
        1000.0,
        settings(leverage=2, open_position_side="Both", trend_mode=trend_mode),
        contract(),
    )
    assert engine.process(
        candle(low=98.0, high=102.0, mark_low=98.0, mark_high=102.0),
        100.0,
        1.0,
    )
    assert set(engine.positions) == {"Short"}
    assert engine.positions["Short"]["entry_price"] == pytest.approx(
        99.0 if trend_mode else 101.0
    )


def test_pending_hedge_expires_without_fill() -> None:
    """Verify pending hedge expires without fill."""
    engine = MarginEngine(
        1000.0,
        settings(
            strategy_mode=HEDGE_MODE,
            exit_band_sigma=1.0,
            hedge_entry_timeout_bars=1,
        ),
        contract(),
    )
    _ = engine.process(candle(), 100.0, 1.0)
    # Only the short order is eligible after an upward gap; the long expires.
    _ = engine.process(
        candle(
            1,
            open=102.0,
            low=102.0,
            high=102.0,
            close=102.0,
            mark_open=102.0,
            mark_low=102.0,
            mark_high=102.0,
            mark_close=102.0,
        ),
        100.0,
        1.0,
    )
    assert not engine.pending
    assert set(engine.positions) == {"Short"}


@pytest.mark.parametrize(
    "change",
    [
        {"max_leverage": 0},
        {"min_notional": -1},
        {"tiers": []},
        {
            "tiers": [{
                "limit": 100.0,
                "mmr": -0.01,
                "deduction": 0.0,
                "max_leverage": 100.0,
            }]
        },
    ],
)
def test_invalid_exchange_metadata_is_rejected(
    change: dict[str, object],
) -> None:
    """Verify invalid exchange metadata is rejected."""
    with pytest.raises(ValueError, match=r"contract|tier"):
        validate_contract({**contract(), **change})


def test_api_error_and_empty_or_spot_instrument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify api error and empty or spot instrument."""
    response = ErrorResponse()
    monkeypatch.setattr(requests, "get", ignoring_arguments(lambda: response))
    with pytest.raises(ValueError, match="bad symbol"):
        _ = data.request_rows("instruments-info")
    monkeypatch.setattr(
        data,
        "request_rows",
        ignoring_arguments(lambda: {"list": list[object]()}),
    )
    with pytest.raises(ValueError, match="No Bybit instrument"):
        _ = data.fetch_contract("MISSING")
    monkeypatch.setattr(
        data,
        "request_rows",
        ignoring_arguments(lambda: {"list": [{"contractType": "Spot"}]}),
    )
    with pytest.raises(ValueError, match="perpetual"):
        _ = data.fetch_contract("BTCUSDT")


def test_mark_pagination_and_funding_gap_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify mark pagination and funding gap detection."""
    start = int(candle().time.timestamp() * 1000)
    pages = iter([
        {"list": [[str(start + 60000), "100", "101", "99", "100"]]},
        {"list": [[str(start), "100", "101", "99", "100"]]},
    ])
    monkeypatch.setattr(
        data, "request_rows", ignoring_arguments(lambda: next(pages))
    )
    marks = data.fetch_mark_prices("BTCUSDT", "1m", start, end_ms=start + 60000)
    assert pd.to_datetime(marks["time"], utc=True).tolist() == [
        candle().time,
        candle(1).time,
    ]

    def gap(*_args: object, **kwargs: str | int) -> dict[str, object]:
        if integer(kwargs["endTime"]) < start:
            return {"list": list[object]()}
        return {
            "list": [
                {"fundingRateTimestamp": str(start), "fundingRate": ".001"}
            ]
        }

    monkeypatch.setattr(data, "request_rows", gap)
    with pytest.raises(ValueError, match="Incomplete funding"):
        _ = data.fetch_funding(
            "BTCUSDT",
            start,
            start + 9 * 3600000,
            interval_minutes=480,
        )


@pytest.mark.parametrize("blank", ["", "  ", None])
def test_empty_bybit_deductions_are_derived_for_allparse_risk_tiers(
    blank: str | None,
) -> None:
    """Verify empty bybit deductions are derived for allparse risk tiers."""
    # API order is not assumed. Higher tiers require nonzero deductions.
    rows = [
        {
            "riskLimitValue": "200000",
            "maintenanceMargin": ".01",
            "mmDeduction": blank,
            "maxLeverage": "50",
        },
        {
            "riskLimitValue": "100000",
            "maintenanceMargin": ".005",
            "mmDeduction": blank,
            "maxLeverage": "100",
        },
        {
            "riskLimitValue": "300000",
            "maintenanceMargin": ".02",
            "mmDeduction": blank,
            "maxLeverage": "25",
        },
    ]
    tiers = parse_risk_tiers(rows, "XRPUSDT")
    assert [t["deduction"] for t in tiers] == pytest.approx([0, 500, 2500])
    # Maintenance is continuous at the tier boundaries.
    for before, after in itertools.pairwise(tiers):
        boundary = before["limit"]
        assert boundary * before["mmr"] - before["deduction"] == pytest.approx(
            boundary * after["mmr"] - after["deduction"]
        )


def test_omitted_deduction_uses_previous_published_deduction() -> None:
    """Verify omitted deduction uses previous published deduction."""
    rows = [
        {
            "riskLimitValue": "100000",
            "maintenanceMargin": ".005",
            "maxLeverage": "100",
        },
        {
            "riskLimitValue": "200000",
            "maintenanceMargin": ".01",
            "mmDeduction": "500",
            "maxLeverage": "50",
        },
        {
            "riskLimitValue": "300000",
            "maintenanceMargin": ".02",
            "maxLeverage": "25",
        },
    ]
    assert [
        t["deduction"] for t in parse_risk_tiers(rows, "XRPUSDT")
    ] == pytest.approx([0, 500, 2500])


@pytest.mark.parametrize(
    "field", ["maintenanceMargin", "riskLimitValue", "maxLeverage"]
)
def test_missing_required_risk_fields_name_the_market_and_field(
    field: str,
) -> None:
    """Verify missing required risk fields name the market and field."""
    row = {
        "riskLimitValue": "100000",
        "maintenanceMargin": ".005",
        "maxLeverage": "100",
    }
    row[field] = ""
    with pytest.raises(
        ValueError, match=f"XRPUSDT: missing or invalid {field}"
    ):
        _ = parse_risk_tiers([row], "XRPUSDT")


def test_invalid_nonempty_deduction_is_not_silently_defaulted() -> None:
    """Verify invalid nonempty deduction is not silently defaulted."""
    with pytest.raises(ValueError, match="mmDeduction"):
        _ = parse_risk_tiers(
            [{
                "riskLimitValue": "100000",
                "maintenanceMargin": ".005",
                "maxLeverage": "100",
                "mmDeduction": "bad",
            }],
            "XRPUSDT",
        )
