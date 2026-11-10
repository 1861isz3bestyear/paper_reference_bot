# Copyright 2026 avwap-workspace-tests contributors
"""Export a consistent archive or download one from a VPS over SSH."""

import argparse
import asyncio
import shutil
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from market_storage.backup import snapshot
from market_storage.transfer import Remote, pull


class Arguments(argparse.Namespace):
    """Typed command destinations."""

    command: str = ""
    archive: Path = Path("tunnel_inputs.sqlite")
    host: str = ""
    remote_root: str = ""
    remote_database: str = "server_bot/tunnel_inputs.sqlite"
    output: Path = Path("tunnel_inputs.sqlite")


def export_archive(path: Path) -> None:
    """Stream a consistent database backup without loading it into memory."""
    with TemporaryDirectory(prefix="market-backup-") as directory:
        backup = Path(directory) / "snapshot.sqlite"
        snapshot(path, backup)
        with backup.open("rb") as source:
            shutil.copyfileobj(source, sys.stdout.buffer)


def main() -> int:
    """Dispatch transfer commands without importing any calculation modules.

    Returns:
        Zero on a complete export/download, one on operational failure.

    """
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser(
        "export", help="Write an online backup to stdout"
    )
    _ = export.add_argument("--archive", type=Path, required=True)
    download = commands.add_parser(
        "pull", help="Download a VPS archive atomically"
    )
    _ = download.add_argument("--host", required=True)
    _ = download.add_argument("--remote-root", required=True)
    _ = download.add_argument(
        "--remote-database", default="server_bot/tunnel_inputs.sqlite"
    )
    _ = download.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(namespace=Arguments())
    try:
        if args.command == "export":
            export_archive(args.archive)
        else:
            remote = Remote(
                host=args.host,
                root=args.remote_root,
                database=args.remote_database,
            )
            asyncio.run(pull(remote, args.output))
    except (
        OSError,
        sqlite3.Error,
        ValueError,
        RuntimeError,
        TimeoutError,
    ) as exc:
        _ = sys.stderr.write(f"Archive transfer failed: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
