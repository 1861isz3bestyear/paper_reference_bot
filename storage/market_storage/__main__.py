# Copyright 2026 avwap-workspace-tests contributors
"""Export a consistent archive or download one from a VPS over SSH."""

import argparse
import asyncio
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from market_storage.backup import snapshot, validate
from market_storage.merge import check, format_report, merge
from market_storage.receipts import acknowledge, cleanup, enable_copy_retention
from market_storage.transfer import Remote, pull


class Arguments(argparse.Namespace):
    """Typed command destinations."""

    command: str = ""
    copy_managed: bool = False
    archive: Path = Path("tunnel_inputs.sqlite")
    host: str = ""
    remote_root: str = ""
    remote_database: str = "server_bot/tunnel_inputs.sqlite"
    source: Path = Path("tunnel_inputs.sqlite")
    output: Path = Path("tunnel_inputs.sqlite")


def export_archive(path: Path) -> None:
    """Stream a consistent database backup without loading it into memory."""
    with TemporaryDirectory(prefix="market-backup-") as directory:
        backup = Path(directory) / "snapshot.sqlite"
        snapshot(path, backup)
        with backup.open("rb") as source:
            shutil.copyfileobj(source, sys.stdout.buffer)


def dispatch(args: Arguments) -> None:
    """Execute the selected storage operation."""
    if args.command == "export":
        if args.copy_managed:
            validate(args.archive)
            enable_copy_retention(args.archive)
        export_archive(args.archive)
    elif args.command == "acknowledge":
        count = acknowledge(
            args.archive, sys.stdin.read(), now=datetime.now(UTC)
        )
        _ = sys.stdout.write(f"Acknowledged {count} copied observations\n")
    elif args.command == "check":
        _ = sys.stdout.write(format_report(check(args.archive)) + "\n")
    elif args.command == "cleanup":
        count = cleanup(args.archive, now=datetime.now(UTC))
        _ = sys.stdout.write(f"Deleted {count} copied observations\n")
    elif args.command == "merge":
        _ = sys.stdout.write(
            format_report(merge(args.source, args.output)) + "\n"
        )
    else:
        remote = Remote(
            host=args.host,
            root=args.remote_root,
            database=args.remote_database,
        )
        report = asyncio.run(pull(remote, args.output))
        _ = sys.stdout.write(format_report(report) + "\n")


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
    _ = export.add_argument("--copy-managed", action="store_true")
    receipt = commands.add_parser(
        "acknowledge", help="Queue copied rows for cleanup"
    )
    _ = receipt.add_argument("--archive", type=Path, required=True)
    download = commands.add_parser(
        "pull", help="Download a VPS archive atomically"
    )
    _ = download.add_argument("--host", required=True)
    _ = download.add_argument("--remote-root", required=True)
    _ = download.add_argument(
        "--remote-database", default="server_bot/tunnel_inputs.sqlite"
    )
    _ = download.add_argument("--output", type=Path, required=True)
    combine = commands.add_parser(
        "merge", help="Validate and merge local archives"
    )
    _ = combine.add_argument("--source", type=Path, required=True)
    _ = combine.add_argument("--output", type=Path, required=True)
    for name in ("check", "cleanup"):
        operation = commands.add_parser(name)
        _ = operation.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args(namespace=Arguments())
    try:
        dispatch(args)
    except (
        OSError,
        sqlite3.Error,
        ValueError,
        TypeError,
        RuntimeError,
        TimeoutError,
    ) as exc:
        _ = sys.stderr.write(f"Archive transfer failed: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
