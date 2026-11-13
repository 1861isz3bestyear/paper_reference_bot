# Copyright 2026 avwap-workspace-tests contributors
"""Acknowledged-only 48-hour cleanup and immutable retry deadlines."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from market_storage.archive import Archive
from market_storage.receipts import acknowledge, cleanup, manifest
from tests.conftest import STAMP

if TYPE_CHECKING:
    from pathlib import Path

    from market_storage.models import Observation


def test_receipt_deadline_preserves_uncopied_rows(
    tmp_path: Path, observation: Observation
) -> None:
    """Old age alone cannot delete uncopied rows; both storage modes expire."""
    path = tmp_path / "vps.sqlite"
    archive = Archive(path=path)
    archive.store(observation)
    Archive(path=path, mode="raw").store(observation)
    content = manifest(path).decode()
    uncopied = replace(
        observation,
        requested=STAMP + timedelta(minutes=5),
        available=STAMP + timedelta(minutes=5),
        collection="uncopied",
    )
    archive.store(uncopied)
    assert acknowledge(path, content, now=STAMP) == 1
    assert acknowledge(path, content, now=STAMP + timedelta(hours=1)) == 1
    assert cleanup(path, now=STAMP + timedelta(hours=48, microseconds=-1)) == 0
    archive.prune(
        now=STAMP + timedelta(hours=48), retention=timedelta(hours=24)
    )
    with closing(sqlite3.connect(path)) as db:
        assert db.execute(
            "SELECT count(*) FROM tunnel_observations"
        ).fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM tunnel_inputs").fetchone() == (
            0,
        )
        assert db.execute(
            "SELECT count(*) FROM tunnel_payloads"
        ).fetchone() == (1,)
    assert cleanup(path, now=STAMP + timedelta(days=3)) == 0


def test_bad_or_stale_receipt_does_not_mark_new_content(
    tmp_path: Path, observation: Observation
) -> None:
    """A forged or stale fingerprint cannot queue a modified response."""
    path = tmp_path / "vps.sqlite"
    Archive(path=path).store(observation)
    content = manifest(path).decode()
    with pytest.raises(ValueError, match="fingerprint"):
        _ = acknowledge(path, "not a digest", now=STAMP)
    path.unlink()
    Archive(path=path).store(replace(observation, payload='{"changed":true}'))
    assert acknowledge(path, content, now=STAMP) == 0
    assert cleanup(path, now=STAMP + timedelta(days=3)) == 0
