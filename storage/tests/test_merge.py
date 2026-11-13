# Copyright 2026 avwap-workspace-tests contributors
"""Check cumulative history, corruption rejection and missing times."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from market_storage.archive import Archive
from market_storage.merge import merge
from market_storage.models import Source, Window
from tests.conftest import STAMP

if TYPE_CHECKING:
    from pathlib import Path

    from market_storage.models import Observation


def test_overlap_preserves_old_history_and_gaps(
    tmp_path: Path, observation: Observation
) -> None:
    """Remove repeats while keeping revisions and failed attempts."""
    source, local = tmp_path / "remote.sqlite", tmp_path / "local.sqlite"
    Archive(path=local).store(observation)
    raw = Archive(path=source, mode="raw")
    raw.store(observation)
    raw.store(replace(observation, collection="duplicate"))
    later = replace(
        observation,
        requested=STAMP + timedelta(minutes=10),
        available=STAMP + timedelta(minutes=10),
        collection="later",
    )
    raw.store(later)
    raw.store(
        replace(later, error="ConnectionError", payload="", collection="failed")
    )
    report = merge(source, local)
    expected_observations, expected_repeats = 3, 2
    assert report.observations == expected_observations
    assert report.duplicates == expected_repeats
    spot = next(item for item in report.timelines if item.source == Source.SPOT)
    assert spot.missing_buckets == 1
    assert spot.successful_buckets == expected_repeats
    assert Archive(path=local).load(
        Window(asset="BTC", start=STAMP, end=later.available)
    ) == (observation, later)
    assert merge(source, local).observations == expected_observations
    empty = tmp_path / "empty.sqlite"
    with closing(sqlite3.connect(source)) as db, db:
        _ = db.execute("DELETE FROM tunnel_inputs")
    _ = merge(source, local)
    assert merge(source, empty).observations == 0
    assert Archive(path=local).load(
        Window(asset="BTC", start=STAMP, end=later.available)
    ) == (observation, later)


def test_conflict_preserves_published_bytes(
    tmp_path: Path, observation: Observation
) -> None:
    """Reject a valid payload silently rewritten for exactly the same time."""
    source, local = tmp_path / "remote.sqlite", tmp_path / "local.sqlite"
    Archive(path=local).store(observation)
    original = local.read_bytes()
    Archive(path=source).store(replace(observation, payload='{"wrong":1}'))
    with pytest.raises(ValueError, match="Conflicting"):
        _ = merge(source, local)
    assert local.read_bytes() == original


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE tunnel_payloads SET codec='unknown'",
        "UPDATE tunnel_payloads SET payload=X'0001'",
        "UPDATE tunnel_payloads SET digest='wrong'",
        "UPDATE tunnel_observations SET requested_at='2026-11-10T12:00:00'",
        "UPDATE tunnel_observations SET asset='SOL'",
        "UPDATE tunnel_observations SET collection_id=''",
        "UPDATE tunnel_observations SET source='candles'",
        (
            "UPDATE tunnel_observations SET"
            " available_at='2026-11-09T12:00:00+00:00'"
        ),
        "UPDATE tunnel_observations SET minute_at='bad'",
        "UPDATE tunnel_observations SET minute_at='2026-11-10T12:00:01+00:00'",
    ],
)
def test_corrupt_metadata_or_payload_never_publishes(
    tmp_path: Path, observation: Observation, mutation: str
) -> None:
    """Reject logically invalid SQLite even when quick_check passes."""
    source, local = tmp_path / "remote.sqlite", tmp_path / "local.sqlite"
    Archive(path=source).store(observation)
    Archive(path=local).store(observation)
    before = local.read_bytes()
    with closing(sqlite3.connect(source)) as db, db:
        _ = db.execute(mutation)
    with pytest.raises((ValueError, TypeError)):
        _ = merge(source, local)
    assert local.read_bytes() == before


def test_digest_and_json_are_checked(
    tmp_path: Path, observation: Observation
) -> None:
    """Detect a wrong digest even when all references still resolve."""
    source, local = tmp_path / "remote.sqlite", tmp_path / "local.sqlite"
    Archive(path=source).store(observation)
    with closing(sqlite3.connect(source)) as db, db:
        _ = db.execute("UPDATE tunnel_payloads SET digest='wrong'")
        _ = db.execute("UPDATE tunnel_observations SET digest='wrong'")
    with pytest.raises(ValueError, match="digest mismatch"):
        _ = merge(source, local)
    assert not local.exists()
    source.unlink()
    Archive(path=source, mode="raw").store(
        replace(observation, payload="broken JSON")
    )
    with pytest.raises(ValueError, match="Expecting value"):
        _ = merge(source, local)


def test_concurrent_merges_preserve_both_imports(
    tmp_path: Path, observation: Observation
) -> None:
    """Serialize local writers so overlapping imports cannot lose updates."""
    paths = (tmp_path / "first.sqlite", tmp_path / "second.sqlite")
    for index, path in enumerate(paths):
        Archive(path=path).store(
            replace(observation, asset="BTC" if index == 0 else "ETH")
        )
    destination = tmp_path / "local.sqlite"
    with ThreadPoolExecutor() as pool:
        futures = [pool.submit(merge, path, destination) for path in paths]
        for future in futures:
            _ = future.result()
    with closing(sqlite3.connect(destination)) as db:
        assert db.execute(
            "SELECT DISTINCT asset FROM tunnel_observations ORDER BY asset"
        ).fetchall() == [("BTC",), ("ETH",)]
