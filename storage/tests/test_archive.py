# Copyright 2026 avwap-workspace-tests contributors
"""Module tests against real SQLite, compression and persisted cadence."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING, Literal

import pytest

from market_storage.archive import Archive
from market_storage.models import Observation, Source, Window
from tests.conftest import STAMP

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.module


@pytest.mark.parametrize("mode", ["raw", "compact"])
def test_committed_responses_survive_restart_and_cutoffs(
    tmp_path: Path, observation: Observation, mode: Literal["raw", "compact"]
) -> None:
    """Expose only committed successful responses known at calculation time."""
    path = tmp_path / "nested/archive.sqlite"
    archive = Archive(path=path, mode=mode)
    archive.store(observation)
    archive.store(
        replace(
            observation,
            collection="later",
            available=STAMP + timedelta(minutes=5),
        )
    )
    archive.store(replace(observation, error="ValueError", collection="failed"))
    archive.store(replace(observation, asset="ETH", collection="other-asset"))
    window = Window(asset="BTC", start=STAMP, end=STAMP)
    assert Archive(path=path).load(window) == (observation,)
    assert not archive.due("BTC", Source.SPOT, now=STAMP)
    assert archive.due("BTC", Source.SPOT, now=STAMP + timedelta(minutes=5))
    assert archive.due("BTC", Source.OI, now=STAMP)


def test_compression_preserves_unicode_and_deduplicates(
    tmp_path: Path, observation: Observation
) -> None:
    """Store exact repeated JSON once while preserving receipt observations."""
    archive = Archive(path=tmp_path / "archive.sqlite")
    record = replace(observation, payload='{"result":"€"}')
    archive.store(record)
    archive.store(replace(record, collection="second"))
    with closing(sqlite3.connect(archive.path)) as db:
        assert db.execute(
            "SELECT count(*) FROM tunnel_payloads"
        ).fetchone() == (1,)
    window = Window(asset="BTC", start=STAMP, end=STAMP)
    assert (
        tuple(row.payload for row in archive.load(window))
        == (record.payload,) * 2
    )


def test_retention_reclaims_unreferenced_payloads(
    tmp_path: Path, observation: Observation
) -> None:
    """Prune old raw and compact records, retaining the inclusive boundary."""
    path = tmp_path / "archive.sqlite"
    archive = Archive(path=path)
    old = replace(
        observation,
        requested=STAMP - timedelta(days=2),
        available=STAMP - timedelta(days=2),
    )
    archive.store(replace(old, payload="old"))
    Archive(path=path, mode="raw").store(old)
    archive.store(observation)
    archive.prune(now=STAMP + timedelta(days=1), retention=timedelta(days=1))
    assert archive.load(Window(asset="BTC", start=STAMP, end=STAMP)) == (
        observation,
    )
    with closing(sqlite3.connect(path)) as db:
        assert db.execute(
            "SELECT count(*) FROM tunnel_payloads"
        ).fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM tunnel_inputs").fetchone() == (
            0,
        )


def test_missing_archive_is_not_created_by_read_or_schedule(
    tmp_path: Path,
) -> None:
    """Readers must not create empty replacements for missing downloads."""
    archive = Archive(path=tmp_path / "missing.sqlite")
    assert archive.due("BTC", Source.SPOT, now=STAMP)
    archive.prune(now=STAMP, retention=timedelta(days=1))
    with pytest.raises(sqlite3.OperationalError):
        _ = archive.load(Window(asset="BTC", start=STAMP, end=STAMP))
    with pytest.raises(ValueError, match="Retention"):
        archive.prune(now=STAMP, retention=timedelta(0))
    assert not archive.path.exists()


def test_failure_does_not_complete_schedule_bucket(
    tmp_path: Path, observation: Observation
) -> None:
    """An exchange failure remains eligible for retry after process restart."""
    path = tmp_path / "archive.sqlite"
    Archive(path=path).store(replace(observation, error="ValueError"))
    assert Archive(path=path).due("BTC", Source.SPOT, now=STAMP)


def test_legacy_schema_is_upgraded_without_inventing_receipt_times(
    tmp_path: Path, observation: Observation
) -> None:
    """Keep old rows intact and ignore prohibited historical OHLCV inputs."""
    path = tmp_path / "archive.sqlite"
    with closing(sqlite3.connect(path)) as db, db:
        _ = db.execute("""CREATE TABLE tunnel_inputs (
            id INTEGER PRIMARY KEY, asset TEXT, source TEXT, requested_at TEXT,
            available_at TEXT, payload TEXT, error TEXT)""")
        _ = db.execute(
            "INSERT INTO tunnel_inputs VALUES (1,'BTC','candles','','','{}','')"
        )
    archive = Archive(path=path)
    archive.store(observation)
    assert archive.load(Window(asset="BTC", start=STAMP, end=STAMP)) == (
        observation,
    )
    with closing(sqlite3.connect(path)) as db:
        assert db.execute(
            "SELECT collection_id FROM tunnel_inputs WHERE id=1"
        ).fetchone() == (None,)
