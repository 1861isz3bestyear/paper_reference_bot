# Copyright 2026 avwap-workspace-tests contributors
"""Save dated, validated SSH archive snapshots and gzip copies locally."""

import argparse
import asyncio
import gzip
import os
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from market_storage.transfer import Remote, pull


class Arguments(argparse.Namespace):
    """Validated command-line destinations."""

    def __init__(self) -> None:
        """Initialize typed settings from the caller's environment."""
        super().__init__()
        self.host: str = os.environ.get("GEX_HOST", "")
        self.remote_root: str = os.environ.get("GEX_REMOTE_ROOT", "")
        self.remote_database: str = os.environ.get(
            "GEX_REMOTE_DATABASE", "server_bot/tunnel_inputs.sqlite"
        )
        self.output_dir: Path = Path(
            os.environ.get("GEX_OUTPUT_DIR", "downloads")
        )


def compress_snapshot(source: Path) -> Path:
    """Atomically publish a gzip copy while retaining the SQLite snapshot.

    Returns:
        The completed compressed snapshot path.

    """
    destination = source.with_suffix(source.suffix + ".gz")
    with NamedTemporaryFile(dir=source.parent, delete=False) as output:
        staging = Path(output.name)
        try:
            with (
                source.open("rb") as original,
                gzip.GzipFile(fileobj=output, mode="wb", filename="") as zipped,
            ):
                shutil.copyfileobj(original, zipped)
            output.flush()
            os.fsync(output.fileno())
            _ = staging.replace(destination)
        finally:
            staging.unlink(missing_ok=True)
    return destination


async def download_snapshot(remote: Remote, directory: Path) -> Path:
    """Download and validate a unique UTC snapshot, then compress a copy.

    Returns:
        The retained SQLite snapshot for calculator use.

    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = directory / f"tunnel_inputs-{stamp}.sqlite"
    await pull(remote, destination)
    _ = await asyncio.to_thread(compress_snapshot, destination)
    return destination


def main() -> int:
    """Download using explicit arguments or persistent environment settings.

    Returns:
        Zero on success or one on an operational failure.

    """
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--host", default=os.environ.get("GEX_HOST", ""))
    _ = parser.add_argument(
        "--remote-root", default=os.environ.get("GEX_REMOTE_ROOT", "")
    )
    _ = parser.add_argument(
        "--remote-database",
        default=os.environ.get(
            "GEX_REMOTE_DATABASE", "server_bot/tunnel_inputs.sqlite"
        ),
    )
    _ = parser.add_argument(
        "--output-dir",
        type=Path,
        default=os.environ.get("GEX_OUTPUT_DIR", "downloads"),
    )
    args = parser.parse_args(namespace=Arguments())
    if not args.host or not args.remote_root:
        parser.error("Set GEX_HOST and GEX_REMOTE_ROOT or pass their options.")
    try:
        remote = Remote(
            host=args.host,
            root=args.remote_root,
            database=args.remote_database,
        )
        destination = asyncio.run(download_snapshot(remote, args.output_dir))
    except (
        OSError,
        sqlite3.Error,
        ValueError,
        RuntimeError,
        TimeoutError,
    ) as exc:
        _ = sys.stderr.write(f"Archive download failed: {exc}\n")
        return 1
    _ = sys.stdout.write(f"Saved {destination}\nCompressed {destination}.gz\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
