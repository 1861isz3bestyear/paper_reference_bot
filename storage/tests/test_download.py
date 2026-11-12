# Copyright 2026 avwap-workspace-tests contributors
"""Verify repeatable snapshot downloads and recoverable compression failures."""

import gzip
import sys
from typing import TYPE_CHECKING

import pytest

from market_storage import download
from market_storage.archive import Archive
from market_storage.models import Window
from tests.conftest import STAMP
from tests.test_transfer import remote_archive as remote_archive
from tests.test_transfer import ssh_boundary as ssh_boundary

if TYPE_CHECKING:
    from pathlib import Path

    from market_storage.models import Observation
    from market_storage.transfer import Remote


@pytest.mark.usefixtures("ssh_boundary")
def test_dated_downloads_preserve_data(
    tmp_path: Path,
    remote_archive: Remote,
    observation: Observation,
    *,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep successive validated snapshots and lossless compressed copies."""
    directory = tmp_path / "downloads"
    monkeypatch.setenv("GEX_HOST", remote_archive.host)
    monkeypatch.setenv("GEX_REMOTE_ROOT", remote_archive.root)
    monkeypatch.setenv("GEX_OUTPUT_DIR", str(directory))
    monkeypatch.setattr(sys, "argv", ["gex-pull"])
    expected_snapshots = 2
    for _ in range(expected_snapshots):
        assert download.main() == 0
    snapshots = list(directory.glob("*.sqlite"))
    assert len(snapshots) == expected_snapshots
    for path in snapshots:
        assert Archive(path=path).load(
            Window(asset="BTC", start=STAMP, end=STAMP)
        ) == (observation,)
        with gzip.open(str(path) + ".gz", "rb") as compressed:
            assert compressed.read() == path.read_bytes()


@pytest.mark.usefixtures("ssh_boundary")
def test_download_failure_leaves_no_snapshot(
    tmp_path: Path, remote_archive: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed scheduled run exits nonzero without retaining partial data."""
    directory = tmp_path / "downloads"
    monkeypatch.setenv("SSH_FIXTURE_MODE", "fail")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gex-pull",
            "--host",
            remote_archive.host,
            "--remote-root",
            remote_archive.root,
            "--output-dir",
            str(directory),
        ],
    )
    assert download.main() == 1
    assert not list(directory.iterdir())


def test_missing_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Require an actual SSH destination before doing any work."""
    monkeypatch.delenv("GEX_HOST", raising=False)
    monkeypatch.delenv("GEX_REMOTE_ROOT", raising=False)
    monkeypatch.setattr(sys, "argv", ["gex-pull"])
    with pytest.raises(SystemExit, match="2"):
        _ = download.main()


def test_compression_failure_preserves_source(tmp_path: Path) -> None:
    """Remove staging files when the source cannot be read."""
    with pytest.raises(FileNotFoundError):
        _ = download.compress_snapshot(tmp_path / "missing.sqlite")
    assert not list(tmp_path.iterdir())
