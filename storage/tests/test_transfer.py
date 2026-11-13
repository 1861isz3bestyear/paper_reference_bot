# Copyright 2026 avwap-workspace-tests contributors
"""Integration of live SQLite backup and atomic local replacement."""

import asyncio
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from market_storage import __main__ as cli
from market_storage.archive import Archive
from market_storage.backup import snapshot, validate
from market_storage.models import Window
from market_storage.transfer import Remote, pull
from tests.conftest import STAMP

if TYPE_CHECKING:
    from market_storage.models import Observation

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def ssh_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Execute remote commands locally at the SSH network boundary."""
    executable = tmp_path / "ssh"
    _ = executable.write_text(
        f"#!{sys.executable}\nimport os, sys\nmode ="
        " os.environ.get('SSH_FIXTURE_MODE', '')\nif mode == 'fail':"
        " sys.exit(23)\nif mode == 'ack-fail' and 'acknowledge' in"
        " sys.argv[-1]: sys.exit(23)\nif mode == 'corrupt':\n   "
        " print('incomplete database')\n    sys.exit(0)\nos.execv('/bin/sh',"
        " ['sh', '-c', sys.argv[-1]])\n"
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])


@pytest.fixture
def remote_archive(tmp_path: Path, observation: Observation) -> Remote:
    """Provide an isolated deployment with a real independent storage CLI.

    Returns:
        A local fixture representing the remote deployment layout.

    """
    root = tmp_path / "remote deployment"
    root.mkdir()
    (root / "storage").symlink_to(ROOT, target_is_directory=True)
    remote = Remote(host="fixture-vps", root=str(root))
    Archive(path=root / remote.database).store(observation)
    return remote


def test_online_backup_includes_committed_wal(
    tmp_path: Path, observation: Observation
) -> None:
    """Back up a still-open WAL database rather than copying its main file."""
    source = tmp_path / "source.sqlite"
    Archive(path=source).store(observation)
    with closing(sqlite3.connect(source)) as writer:
        _ = writer.execute("PRAGMA journal_mode=WAL")
        with writer:
            _ = writer.execute(
                "UPDATE tunnel_observations SET collection_id='wal-commit'"
            )
        destination = tmp_path / "backup.sqlite"
        snapshot(source, destination)
    rows = Archive(path=destination).load(
        Window(asset="BTC", start=STAMP, end=STAMP)
    )
    assert rows[0].collection == "wal-commit"
    with pytest.raises(ValueError, match="differ"):
        snapshot(source, source)


@pytest.mark.usefixtures("ssh_boundary")
def test_download_loads_only_complete_validated_backup(
    tmp_path: Path, remote_archive: Remote, observation: Observation
) -> None:
    """Download a real remote CLI export and load it through the storage API."""
    destination = tmp_path / "local/archive.sqlite"
    _ = asyncio.run(pull(remote_archive, destination))
    window = Window(asset="BTC", start=STAMP, end=STAMP)
    assert Archive(path=destination).load(window) == (observation,)
    assert set(destination.parent.iterdir()) == {
        destination,
        destination.with_suffix(".sqlite.lock"),
    }


@pytest.mark.usefixtures("ssh_boundary")
@pytest.mark.parametrize("mode", ["fail", "corrupt"])
def test_failed_download_preserves_existing_archive(
    tmp_path: Path,
    remote_archive: Remote,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str,
) -> None:
    """Never publish a failed or truncated transfer over known local data."""
    destination = tmp_path / "archive.sqlite"
    _ = destination.write_bytes(b"previous local data")
    monkeypatch.setenv("SSH_FIXTURE_MODE", mode)
    with pytest.raises(
        (RuntimeError, sqlite3.DatabaseError), match=r"SSH|database"
    ):
        _ = asyncio.run(pull(remote_archive, destination))
    assert destination.read_bytes() == b"previous local data"


def test_unrelated_sqlite_database_is_rejected(tmp_path: Path) -> None:
    """SQLite integrity alone does not establish an archive schema."""
    path = tmp_path / "unrelated.sqlite"
    with closing(sqlite3.connect(path)) as db:
        _ = db.execute("CREATE TABLE unrelated (id INTEGER)")
    with pytest.raises(ValueError, match="not a market archive"):
        validate(path)


@pytest.mark.usefixtures("ssh_boundary")
def test_inprocess_cli_pull_loads_same_independent_storage(
    tmp_path: Path, remote_archive: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise CLI dispatch and the independently executed remote export."""
    output = tmp_path / "local.sqlite"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "market_storage",
            "pull",
            "--host",
            remote_archive.host,
            "--remote-root",
            remote_archive.root,
            "--output",
            str(output),
        ],
    )
    assert cli.main() == 0
    validate(output)


def test_incomplete_market_schema_is_rejected(tmp_path: Path) -> None:
    """Reject a named market table whose required fields are absent."""
    path = tmp_path / "partial.sqlite"
    with closing(sqlite3.connect(path)) as db:
        _ = db.execute("CREATE TABLE tunnel_inputs (id INTEGER)")
    with pytest.raises(ValueError, match="incomplete"):
        validate(path)


def test_missing_compressed_payload_is_rejected(
    tmp_path: Path, observation: Observation
) -> None:
    """A valid SQLite file with dangling payload references is not complete."""
    path = tmp_path / "incomplete.sqlite"
    Archive(path=path).store(observation)
    with closing(sqlite3.connect(path)) as db, db:
        _ = db.execute("DELETE FROM tunnel_payloads")
    with pytest.raises(ValueError, match="missing compressed"):
        validate(path)


@pytest.mark.usefixtures("ssh_boundary")
def test_ack_failure_preserves_local_copy_and_remote_data(
    tmp_path: Path, remote_archive: Remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never roll back durable history or queue deletion when SSH ACK fails."""
    destination = tmp_path / "local.sqlite"
    monkeypatch.setenv("SSH_FIXTURE_MODE", "ack-fail")
    with pytest.raises(RuntimeError, match="acknowledgement failed"):
        _ = asyncio.run(pull(remote_archive, destination))
    validate(destination)
    remote = Path(remote_archive.root) / remote_archive.database
    with closing(sqlite3.connect(remote)) as db:
        assert db.execute("SELECT count(*) FROM copy_receipts").fetchone() == (
            0,
        )
    monkeypatch.delenv("SSH_FIXTURE_MODE")
    _ = asyncio.run(pull(remote_archive, destination))
    with closing(sqlite3.connect(remote)) as db:
        assert db.execute("SELECT count(*) FROM copy_receipts").fetchone() == (
            1,
        )
