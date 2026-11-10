# Copyright 2026 avwap-workspace-tests contributors
"""Load committed source evidence before any GEX or oscillator calculation."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from market_storage.models import Observation, Source

if TYPE_CHECKING:
    from datetime import datetime

    from market_storage.archive import Archive
    from market_storage.models import Window

GEX_SOURCES = frozenset((Source.INSTRUMENTS, Source.OPTIONS, Source.SPOT))


@dataclass(frozen=True, kw_only=True)
class GexInputs:
    """A complete GEX observation from one collection."""

    instruments: Observation
    options: Observation
    spot: Observation

    @property
    def available(self) -> datetime:
        """The first time all three persisted responses were available."""
        return max(
            self.instruments.available,
            self.options.available,
            self.spot.available,
        )


@dataclass(frozen=True, kw_only=True)
class CalculationInputs:
    """Committed responses whose field schemas are validated by consumers."""

    gex: tuple[GexInputs, ...]
    observations: tuple[Observation, ...]

    def stream(self, source: Source) -> tuple[Observation, ...]:
        """Select one chronological, receipt-bounded input stream.

        Returns:
            Successful persisted evidence for the requested source.

        """
        return tuple(row for row in self.observations if row.source == source)


def load_inputs(archive: Archive, window: Window) -> CalculationInputs:
    """Load durable observations and omit incomplete or failed GEX collections.

    Returns:
        Source evidence available at the requested historical cutoff.

    Raises:
        ValueError: If a collection contains conflicting source observations.

    """
    observations = archive.load(window)
    groups: dict[str, dict[Source, Observation]] = {}
    for observation in observations:
        if observation.source not in GEX_SOURCES:
            continue
        group = groups.setdefault(observation.collection, {})
        if observation.source in group:
            msg = "Duplicate source in a GEX collection."
            raise ValueError(msg)
        group[observation.source] = observation
    packets = tuple(
        GexInputs(
            instruments=group[Source.INSTRUMENTS],
            options=group[Source.OPTIONS],
            spot=group[Source.SPOT],
        )
        for group in groups.values()
        if group.keys() >= GEX_SOURCES
    )
    return CalculationInputs(
        gex=tuple(sorted(packets, key=lambda packet: packet.available)),
        observations=observations,
    )
