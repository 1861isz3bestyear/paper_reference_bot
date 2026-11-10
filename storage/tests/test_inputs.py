# Copyright 2026 avwap-workspace-tests contributors
"""Keep GEX calculations downstream of complete, committed observations."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from market_storage.archive import Archive
from market_storage.inputs import load_inputs
from market_storage.models import Observation, Source, Window
from tests.conftest import STAMP

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration


def test_gex_inputs_require_same_complete_successful_collection(
    tmp_path: Path, observation: Observation
) -> None:
    """Never manufacture a complete GEX chain from unrelated partial polls."""
    archive = Archive(path=tmp_path / "archive.sqlite")
    window = Window(asset="BTC", start=STAMP, end=STAMP + timedelta(minutes=1))
    archive.store(replace(observation, source=Source.INSTRUMENTS))
    archive.store(
        replace(observation, source=Source.OPTIONS, collection="other")
    )
    archive.store(observation)
    assert load_inputs(archive, window).gex == ()
    archive.store(
        replace(observation, source=Source.OPTIONS, available=window.end)
    )
    inputs = load_inputs(archive, window)
    assert len(inputs.gex) == 1
    assert inputs.gex[0].available == window.end
    assert inputs.stream(Source.SPOT) == (observation,)
    assert (
        load_inputs(archive, Window(asset="BTC", start=STAMP, end=STAMP)).gex
        == ()
    )
    archive.store(replace(observation, source=Source.OPTIONS))
    with pytest.raises(ValueError, match="Duplicate source"):
        _ = load_inputs(archive, window)


def test_ungrouped_legacy_rows_remain_readable_without_false_gex_grouping(
    tmp_path: Path, observation: Observation
) -> None:
    """Keep old receipt times but never invent shared collection provenance."""
    path = tmp_path / "legacy.sqlite"
    archive = Archive(path=path, mode="raw")
    for source in (Source.INSTRUMENTS, Source.OPTIONS, Source.SPOT):
        archive.store(replace(observation, source=source))
    with closing(sqlite3.connect(path)) as db, db:
        _ = db.execute("UPDATE tunnel_inputs SET collection_id=NULL")
    inputs = load_inputs(archive, Window(asset="BTC", start=STAMP, end=STAMP))
    expected_rows = 3
    assert len(inputs.observations) == expected_rows
    assert len({row.collection for row in inputs.observations}) == expected_rows
    assert all(row.available == STAMP for row in inputs.observations)
    assert inputs.gex == ()
