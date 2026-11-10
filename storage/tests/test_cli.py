# Copyright 2026 avwap-workspace-tests contributors
"""Exercise command parsing, backup streaming and operational exit statuses."""

import sys
from typing import TYPE_CHECKING

import pytest

from market_storage import __main__ as cli
from market_storage.archive import Archive
from market_storage.backup import validate
from market_storage.models import Window
from tests.conftest import STAMP

if TYPE_CHECKING:
    from pathlib import Path

    from market_storage.models import Observation


def test_export_streams_readable_online_backup(
    tmp_path: Path,
    observation: Observation,
    monkeypatch: pytest.MonkeyPatch,
    *,
    capsysbinary: pytest.CaptureFixture[bytes],
) -> None:
    """Export binary SQLite without mixing status text into the stream."""
    source = tmp_path / "source.sqlite"
    Archive(path=source).store(observation)
    monkeypatch.setattr(
        sys, "argv", ["market_storage", "export", "--archive", str(source)]
    )
    assert cli.main() == 0
    captured = capsysbinary.readouterr()
    backup = tmp_path / "received.sqlite"
    _ = backup.write_bytes(captured.out)
    validate(backup)
    assert captured.err == b""
    assert Archive(path=backup).load(
        Window(asset="BTC", start=STAMP, end=STAMP)
    ) == (observation,)


def test_export_missing_database_fails_without_creating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report a missing remote archive and emit no partial binary export."""
    missing = tmp_path / "missing.sqlite"
    monkeypatch.setattr(
        sys, "argv", ["market_storage", "export", "--archive", str(missing)]
    )
    assert cli.main() == 1
    captured = capsys.readouterr()
    assert "Archive transfer failed" in captured.err
    assert not captured.out
    assert not missing.exists()


def test_pull_rejects_invalid_host_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validate the destination before starting any transfer process."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "market_storage",
            "pull",
            "--host",
            "-bad",
            "--remote-root",
            "/srv/bot",
            "--output",
            str(tmp_path / "download.sqlite"),
        ],
    )
    with pytest.raises(SystemExit, match="2"):
        _ = cli.main()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "market_storage",
            "pull",
            "--host",
            "vps",
            "--remote-root",
            "relative",
            "--output",
            str(tmp_path / "download.sqlite"),
        ],
    )
    assert cli.main() == 1
