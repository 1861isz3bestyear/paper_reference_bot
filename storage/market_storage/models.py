# Copyright 2026 avwap-workspace-tests contributors
"""Validate market storage records and observation windows."""

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from datetime import datetime

Asset = Literal["BTC", "ETH"]


class Source(StrEnum):
    """Inputs accepted by the archive; candle OHLCV is intentionally absent."""

    INSTRUMENTS = "instruments"
    OPTIONS = "options"
    SPOT = "spot"
    DEPTH = "depth"
    TICKER = "ticker"
    OI = "oi"
    GROWTH_HOURLY = "bitget_growth_24h"
    GROWTH_DAILY = "bitget_growth_30d"
    LOAN_RATE = "bitget_loan_rate"

    @property
    def interval(self) -> int:
        """The source collection period in seconds."""
        return 3600 if self in MARGIN_SOURCES else 300


MARGIN_SOURCES = frozenset(
    (Source.GROWTH_HOURLY, Source.GROWTH_DAILY, Source.LOAN_RATE)
)


def asset_value(value: str) -> Asset:
    """Validate an external asset.

    Returns:
        A supported asset.

    Raises:
        ValueError: If the asset is unsupported.

    """
    if value not in {"BTC", "ETH"}:
        msg = "Asset must be BTC or ETH."
        raise ValueError(msg)
    return "BTC" if value == "BTC" else "ETH"


def aware(value: datetime) -> None:
    """Reject ambiguous observation times.

    Raises:
        ValueError: If the timestamp has no UTC offset.

    """
    if value.utcoffset() is None:
        msg = "Archive timestamps must have a UTC offset."
        raise ValueError(msg)


@dataclass(frozen=True, kw_only=True)
class Observation:
    """Exact response with its request, receipt, and collection identity."""

    asset: Asset
    source: Source
    requested: datetime
    available: datetime
    payload: str
    collection: str
    error: str = ""

    def __post_init__(self) -> None:
        """Validate the temporal and storage identity.

        Raises:
            ValueError: If a record cannot describe a causal observation.

        """
        _ = asset_value(self.asset)
        aware(self.requested)
        aware(self.available)
        if self.available < self.requested or not self.collection:
            msg = (
                "Archive records require causal times and collection identity."
            )
            raise ValueError(msg)


@dataclass(frozen=True, kw_only=True)
class Window:
    """Receipt-time window with an inclusive historical calculation cutoff."""

    asset: Asset
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        """Validate query bounds before opening the archive.

        Raises:
            ValueError: If bounds are inverted or ambiguous.

        """
        _ = asset_value(self.asset)
        aware(self.start)
        aware(self.end)
        if self.start > self.end:
            msg = "Archive window start must not exceed its end."
            raise ValueError(msg)
