# Copyright 2026 avwap-workspace-tests contributors
"""End-to-end CLI backup and transfer with a deterministic SSH boundary."""

import asyncio
import os
import sys
from typing import TYPE_CHECKING

import pytest

from market_storage.archive import Archive
from market_storage.models import Window
from tests.conftest import STAMP
from tests.test_transfer import remote_archive as remote_archive
from tests.test_transfer import ssh_boundary as ssh_boundary

if TYPE_CHECKING:
    from pathlib import Path

    from market_storage.models import Observation
    from market_storage.transfer import Remote

pytestmark = pytest.mark.e2e


async def command(*arguments: str) -> tuple[int, str]:
    """Execute the production entry point in a separate interpreter.

    Returns:
        CLI exit status and captured diagnostics.

    """
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "market_storage",
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )
    _, error = await asyncio.wait_for(process.communicate(), timeout=30)
    return await process.wait(), error.decode()


@pytest.mark.usefixtures("ssh_boundary")
def test_cli_pulls_remote_archive_into_local_calculation_input(
    tmp_path: Path, remote_archive: Remote, observation: Observation
) -> None:
    """Run local CLI, remote export CLI, transfer, validation and local read."""
    output = tmp_path / "download.sqlite"
    code, error = asyncio.run(
        command(
            "pull",
            "--host",
            remote_archive.host,
            "--remote-root",
            remote_archive.root,
            "--output",
            str(output),
        )
    )
    assert code == 0, error
    assert Archive(path=output).load(
        Window(asset="BTC", start=STAMP, end=STAMP)
    ) == (observation,)


@pytest.mark.usefixtures("ssh_boundary")
def test_cli_download_failure_is_nonzero_and_does_not_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remote_archive: Remote
) -> None:
    """Report transport failure to callers without publishing partial data."""
    output = tmp_path / "download.sqlite"
    _ = output.write_bytes(b"previous")
    monkeypatch.setenv("SSH_FIXTURE_MODE", "fail")
    code, error = asyncio.run(
        command(
            "pull",
            "--host",
            remote_archive.host,
            "--remote-root",
            remote_archive.root,
            "--output",
            str(output),
        )
    )
    assert code == 1
    assert "SSH archive download failed" in error
    assert output.read_bytes() == b"previous"
