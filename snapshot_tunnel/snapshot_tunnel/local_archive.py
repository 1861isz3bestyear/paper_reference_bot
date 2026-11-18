# Copyright 2026 anchored-vwam-backtest contributors
"""Load local tunnel replay inputs without CSV conversion or live backfill."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING

from market_storage.archive import Archive
from market_storage.inputs import CalculationInputs, load_inputs
from market_storage.models import Source, Window

from snapshot_tunnel.archive_export import (
    archived_snapshot,
    collection_windows,
)
from snapshot_tunnel.archive_model import LocalArchiveSource, RecordedCollection
from snapshot_tunnel.archived_market import (
    ArchivedOscillators,
    calculate_oscillators,
)
from snapshot_tunnel.tunnel import OiConfirmationMode, oscillator_vote
from snapshot_tunnel.tunnel_flow import (
    OscillatorObservation,
    TunnelFlow,
    momentum_votes,
    oi_confirmation_series,
    oi_signal_direction,
)

if TYPE_CHECKING:
    from pathlib import Path

    from market_storage.models import Observation

    from snapshot_tunnel.oi_divergence import DivergenceSeries
    from snapshot_tunnel.options_model import Snapshot
    from snapshot_tunnel.tunnel import TunnelSettings


def inspect_archive(path: Path) -> LocalArchiveSource:
    """Inspect SQLite metadata before downloading prices or parsing chains.

    Returns:
        Versioned coverage suitable for cached loading and default dates.

    Raises:
        ValueError: If no complete successful BTC chains exist.

    """
    stat = path.stat()
    records = collection_windows(path, asset="BTC")
    if not records:
        msg = "The local archive has no complete successful BTC options chains."
        raise ValueError(msg)
    return LocalArchiveSource(
        path=path,
        modified_ns=stat.st_mtime_ns,
        size=stat.st_size,
        collections=records,
    )


def require_archive_version(source: LocalArchiveSource) -> None:
    """Reject changed files rather than combine data from different downloads.

    Raises:
        ValueError: If the source has been replaced since coverage inspection.

    """
    stat = source.path.stat()
    if (stat.st_mtime_ns, stat.st_size) != (source.modified_ns, source.size):
        msg = (
            "Local archive changed while loading; refresh to load its new"
            " window."
        )
        raise ValueError(msg)


def daily_collections(
    source: LocalArchiveSource,
) -> tuple[RecordedCollection, ...]:
    """Select nearest 08:15 UTC receipts before decompressing full chains.

    Returns:
        At most one complete collection per UTC day, with earlier ties first.

    """
    selected: dict[datetime, RecordedCollection] = {}
    for record in source.collections:
        receipt = record.window.end.astimezone(UTC)
        target = datetime.combine(receipt.date(), time(8, 15), UTC)
        previous = selected.get(target)
        if previous is None or abs(receipt - target) < abs(
            previous.window.end - target
        ):
            selected[target] = record
    return tuple(selected[day] for day in sorted(selected))


def load_archive_snapshots(source: LocalArchiveSource) -> tuple[Snapshot, ...]:
    """Read only the daily chains used by Snapshot tunnel.

    Returns:
        Validated daily chains at their actual availability times.

    """
    require_archive_version(source)
    archive = Archive(path=source.path)
    snapshots: list[Snapshot] = []
    for record in daily_collections(source):
        snapshots.extend(
            archived_snapshot(packet)
            for packet in load_inputs(archive, record.window).gex
            if packet.instruments.collection == record.identity
        )
    require_archive_version(source)
    return tuple(snapshots)


def load_archive_snapshot_at(
    source: LocalArchiveSource, cutoff: datetime
) -> Snapshot | None:
    """Read the latest complete chain available at a chart's cutoff.

    Returns:
        The recorded profile without substituting a daily execution plan.

    """
    require_archive_version(source)
    record = max(
        (item for item in source.collections if item.window.end <= cutoff),
        key=lambda item: item.window.end,
        default=None,
    )
    if record is None:
        return None
    packets = load_inputs(Archive(path=source.path), record.window).gex
    snapshot = next(
        (
            archived_snapshot(packet)
            for packet in packets
            if packet.instruments.collection == record.identity
        ),
        None,
    )
    require_archive_version(source)
    return snapshot


def load_archive_oscillators(source: LocalArchiveSource) -> ArchivedOscillators:
    """Read bounded hourly batches and retain only independent flow evidence.

    Returns:
        Recorded book, margin and OI streams; missing history stays missing.

    """
    require_archive_version(source)
    observations: list[Observation] = []
    start = source.collections[0].window.start
    archive = Archive(path=source.path)
    while start <= source.end:
        end = min(start + timedelta(hours=1), source.end)
        observations.extend(
            archive.load(
                Window(asset="BTC", start=start, end=end),
                sources=frozenset((
                    Source.DEPTH,
                    Source.GROWTH_HOURLY,
                    Source.OI,
                    Source.TICKER,
                )),
            )
        )
        start = end + timedelta(microseconds=1)
    require_archive_version(source)
    return calculate_oscillators(
        CalculationInputs(gex=(), observations=tuple(observations))
    )


def divergence_votes(
    series: DivergenceSeries,
    *,
    mode: OiConfirmationMode = OiConfirmationMode.DIVERGENCE,
) -> tuple[OscillatorObservation, ...]:
    """Convert recorded five-minute OI signals without assuming receipt times.

    Returns:
        Causal directional votes, with warmup and gaps explicitly unavailable.

    """
    if mode == OiConfirmationMode.CONVERGENCE:
        series = oi_confirmation_series(
            tuple(point.sample for point in series.points),
            mode=mode,
            interval=timedelta(minutes=5),
        )
    signals = {signal.detected: signal for signal in series.signals}
    direction = 0
    votes: list[OscillatorObservation] = []
    for index, point in enumerate(series.points):
        if series.states[index] is None:
            direction = 0
        signal = signals.get(point.available)
        if signal is not None:
            direction = oi_signal_direction(signal, mode=mode)
        votes.append(
            OscillatorObservation(
                available=point.available,
                direction=direction,
                calculatable=series.states[index] is not None,
            )
        )
    return tuple(votes)


def archive_tunnel_flow(
    streams: ArchivedOscillators, settings: TunnelSettings
) -> TunnelFlow:
    """Apply the chosen voting thresholds to independent archive streams.

    Returns:
        Book, margin and OI confirmations with recorded availability times.

    """
    return TunnelFlow(
        book=tuple(
            OscillatorObservation(
                available=point.available,
                direction=oscillator_vote(point.value, settings),
            )
            for point in streams.order_book
            if point.value is not None
        ),
        margin=tuple(
            OscillatorObservation(
                available=point.available,
                direction=oscillator_vote(point.value, settings),
            )
            for point in streams.margin
            if point.value is not None
        ),
        oi_momentum=momentum_votes(streams.oi),
        oi=divergence_votes(streams.oi, mode=OiConfirmationMode.DIVERGENCE),
        oi_convergence=divergence_votes(
            streams.oi, mode=OiConfirmationMode.CONVERGENCE
        ),
    )
