# Copyright 2026 avwap-workspace-tests contributors
"""Download online SQLite backups through the user's existing SSH access."""

import asyncio
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile

from market_storage.backup import validate


@dataclass(frozen=True, kw_only=True)
class Remote:
    """An SSH destination and absolute deployment root containing storage/."""

    host: str
    root: str
    database: str = "server_bot/tunnel_inputs.sqlite"

    def __post_init__(self) -> None:
        """Validate SSH operands before constructing the remote command.

        Raises:
            ValueError: If a destination could be interpreted as an option.

        """
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*", self.host):
            msg = "Use an SSH host alias or user@hostname."
            raise ValueError(msg)
        if not self.root.startswith("/") or "\n" in self.root:
            msg = "Remote deployment root must be an absolute POSIX path."
            raise ValueError(msg)
        database = PurePosixPath(self.database)
        if database.is_absolute() or ".." in database.parts:
            msg = "Remote database must be relative to the deployment root."
            raise ValueError(msg)

    def command(self) -> list[str]:
        """Build SSH arguments without interpolating user text into shell code.

        Returns:
            A bounded, noninteractive SSH command exporting a live backup.

        """
        root = PurePosixPath(self.root)
        remote = shlex.join([
            "uv",
            "run",
            "--locked",
            "--project",
            str(root / "storage"),
            "python",
            "-m",
            "market_storage",
            "export",
            "--archive",
            str(root / self.database),
        ])
        return [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            self.host,
            'export PATH="$HOME/.local/bin:$PATH"; exec ' + remote,
        ]


async def _download(remote: Remote, output: int) -> None:
    process = await asyncio.create_subprocess_exec(
        *remote.command(), stdout=output, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, error = await asyncio.wait_for(process.communicate(), timeout=300)
    finally:
        if process.returncode is None:
            process.kill()
            _ = await process.wait()
    if await process.wait():
        msg = "SSH archive download failed: " + (error or b"").decode(
            errors="replace"
        )
        raise RuntimeError(msg)


def _publish(staging: Path, destination: Path) -> None:
    validate(staging)
    _ = staging.replace(destination)


async def pull(remote: Remote, destination: Path) -> None:
    """Replace local data only after transfer and validation succeed."""
    await asyncio.to_thread(
        destination.parent.mkdir, parents=True, exist_ok=True
    )
    with NamedTemporaryFile(dir=destination.parent, delete=False) as output:
        staging = Path(output.name)
        try:
            await _download(remote, output.fileno())
            output.flush()
            os.fsync(output.fileno())
            await asyncio.to_thread(_publish, staging, destination)
        finally:
            await asyncio.to_thread(staging.unlink, missing_ok=True)
