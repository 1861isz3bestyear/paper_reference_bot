# Copyright 2026 avwap-workspace-tests contributors
"""Size and protect one funded BTC demo position on the exchange grid."""

from __future__ import annotations

import hashlib
import math
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import TYPE_CHECKING, NamedTuple

from bybit_demo_bot.tunnel_journal import PendingEntry
from shared.validation import numeric_string, object_mapping

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bybit_demo_bot.client import BybitDemoClient
    from bybit_demo_bot.tunnel_signals import TunnelDecision
    from shared.paper_config import PaperBotConfig


class Protection(NamedTuple):
    """Exchange stop loss and take profit with distinct domain meanings."""

    stop: Decimal
    target: Decimal


def positive_decimal(value: object) -> Decimal:
    """Validate exchange numeric text before decimal arithmetic.

    Returns:
        A finite positive decimal.

    Raises:
        ValueError: If the external quantity is invalid.

    """
    if numeric_string(value) <= 0:
        msg = "Exchange quantity must be positive."
        raise ValueError(msg)
    return Decimal(str(value))


def protection(
    entry: Decimal, intent: PendingEntry, *, config: PaperBotConfig
) -> Protection:
    """Recompute percentage protection from the confirmed fill, retaining cap.

    Returns:
        Stop loss and take profit before exchange-grid rounding.

    Raises:
        ValueError: If the frozen target cannot produce a profitable exit.

    """
    direction = Decimal(1 if intent.side == "Buy" else -1)
    stop = entry * (1 - direction * Decimal(str(config.stop_loss_pct)) / 100)
    target = entry * (
        1 + direction * Decimal(str(config.take_profit_pct)) / 100
    )
    if intent.ceiling is not None and intent.side == "Buy":
        target = min(target, intent.ceiling)
    if (target - entry) * direction <= 0 or stop <= 0:
        msg = "Frozen tunnel target is not profitable from the actual fill."
        raise ValueError(msg)
    return Protection(stop=stop, target=target)


def round_protection(
    prices: Protection, tick: Decimal, *, side: str
) -> Protection:
    """Round protection inward so a long target never exceeds its cap.

    Returns:
        Exchange-grid stop and target prices.

    """
    stop, target = prices
    stop_round, target_round = (
        (ROUND_UP, ROUND_DOWN) if side == "Buy" else (ROUND_DOWN, ROUND_UP)
    )
    return Protection(
        stop=(stop / tick).to_integral_value(rounding=stop_round) * tick,
        target=(target / tick).to_integral_value(rounding=target_round) * tick,
    )


def sized_entry(
    client: BybitDemoClient,
    decision: TunnelDecision,
    *,
    config: PaperBotConfig,
    candle: str,
    fingerprint: str,
) -> PendingEntry:
    """Bound demo exposure by saved capital and available funds.

    Returns:
        A deterministic order intent with exchange-valid size and protection.

    Raises:
        ValueError: If size or rounded protection cannot meet exchange limits.

    """
    info = client.instruments("BTCUSDT")
    limits = object_mapping(info["lotSizeFilter"])
    step = positive_decimal(limits["qtyStep"])
    price = positive_decimal(str(client.last_price("BTCUSDT")))
    allocation = min(
        Decimal(str(config.initial_capital)),
        positive_decimal(str(client.available_usdt())) * Decimal("0.9"),
    )
    quantity = (allocation / price / step).to_integral_value(
        rounding=ROUND_DOWN
    ) * step
    minimum = max(
        Decimal(str(config.minimum_order_size)),
        positive_decimal(limits.get("minNotionalValue", "0.01")),
    )
    if (
        quantity < positive_decimal(limits["minOrderQty"])
        or quantity * price < minimum
    ):
        msg = "Saved capital is below the exchange or configured minimum order."
        raise ValueError(msg)
    if "maxMktOrderQty" in limits and quantity > positive_decimal(
        limits["maxMktOrderQty"]
    ):
        msg = "Saved capital exceeds the exchange market-order size limit."
        raise ValueError(msg)
    side = "Buy" if decision.direction == 1 else "Sell"
    ceiling = (
        Decimal(str(decision.target_ceiling))
        if math.isfinite(decision.target_ceiling)
        else None
    )
    intent = PendingEntry(
        link="gex-"
        + hashlib.sha256((fingerprint + candle).encode()).hexdigest()[:28],
        side=side,
        quantity=quantity,
        stop=price,
        target=price,
        ceiling=ceiling,
    )
    stop, target = round_protection(
        protection(price, intent, config=config), price_tick(info), side=side
    )
    if not (stop < price < target if side == "Buy" else target < price < stop):
        msg = "Protection collapses on the exchange price grid."
        raise ValueError(msg)
    return PendingEntry(
        link=intent.link,
        side=side,
        quantity=quantity,
        stop=stop,
        target=target,
        ceiling=ceiling,
    )


def price_tick(info: Mapping[str, object]) -> Decimal:
    """Validate the instrument price grid.

    Returns:
        The finite positive tick size.

    """
    return positive_decimal(object_mapping(info["priceFilter"])["tickSize"])
