# Copyright 2026 anchored-vwam-backtest contributors
"""Run saver subprocesses against a real local HTTP exchange and SQLite."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import threading
from contextlib import closing
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import time_ns
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qs, urlparse

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

# Only the exchange address is redirected. The child executes the production
# CLI, HTTP client, pagination, validation and persistence without mocks.
LAUNCHER = """
import runpy
import sys
from reference_bot import market
market.BYBIT_KLINES_URL = sys.argv.pop(1)
runpy.run_module('server_bot.data_saver', run_name='__main__')
"""
PROJECT = Path(__file__).resolve().parents[1]
MINUTE_MS = 60_000


@dataclass(kw_only=True)
class ExchangeState:
    """Own deterministic candles and observed HTTP requests."""

    rows: list[list[str]] = field(default_factory=list)
    requests: list[dict[str, list[str]]] = field(default_factory=list)
    status: int = 200
    code: int = 0
    include_open: bool = False


class ExchangeServer(ThreadingHTTPServer):
    """Expose fixture exchange state to request handlers."""

    state: ExchangeState

    def __init__(self) -> None:
        """Bind a private loopback port with no external exchange access."""
        self.state = ExchangeState()
        super().__init__(("127.0.0.1", 0), ExchangeHandler)


class ExchangeHandler(BaseHTTPRequestHandler):
    """Serve Bybit-shaped pages through actual HTTP."""

    def do_GET(self) -> None:
        """Filter pages using the production client's requested boundaries."""
        state = cast("ExchangeServer", self.server).state
        params = parse_qs(urlparse(self.path).query)
        state.requests.append(params)
        start, end = int(params["start"][0]), int(params["end"][0])
        rows = [row for row in state.rows if start <= int(row[0]) <= end]
        if state.include_open and end > max(int(row[0]) for row in state.rows):
            rows.append(candle(end))
        payload = json.dumps({
            "retCode": state.code,
            "retMsg": "fixture exchange response",
            "result": {"list": sorted(rows, reverse=True)[:1000]},
        }).encode()
        self.send_response(state.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        _ = self.wfile.write(payload)


@pytest.fixture
def exchange() -> Iterator[ExchangeServer]:
    """Run a real HTTP server for one test and close its listener afterwards.

    Yields:
        Local exchange server with mutable test-owned response state.

    """
    with ExchangeServer() as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server
        finally:
            server.shutdown()
            worker.join(timeout=5)


def candle(timestamp: int, *, close: str = "11") -> list[str]:
    """Build one exchange candle row.

    Returns:
        Exchange wire fields, including volume and turnover.

    """
    return [str(timestamp), "10", "12", "9", close, "2", "22"]


@dataclass(frozen=True, kw_only=True)
class ProcessResult:
    """Capture a completed subprocess without invoking a shell."""

    returncode: int
    stdout: str
    stderr: str


async def run_process(
    command: Sequence[str],
    *,
    directory: Path,
    environment: Mapping[str, str],
) -> ProcessResult:
    """Run an isolated child with bounded runtime and captured output.

    Returns:
        Process exit status and decoded output.

    """
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=directory,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=20
        )
    finally:
        if process.returncode is None:
            process.kill()
            _ = await process.wait()
    return ProcessResult(
        returncode=await process.wait(),
        stdout=stdout.decode(),
        stderr=stderr.decode(),
    )


def launch(
    server: ExchangeServer, directory: Path, *, options: tuple[str, ...] = ()
) -> ProcessResult:
    """Start an independent production CLI process without trading secrets.

    Returns:
        Captured CLI output and exit status.

    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if not any(word in key.upper() for word in ("BYBIT", "SMTP", "API_KEY"))
    }
    environment["PYTHONPATH"] = str(PROJECT)
    environment["NO_PROXY"] = environment["no_proxy"] = "127.0.0.1,localhost"
    return asyncio.run(
        run_process(
            [
                sys.executable,
                "-c",
                LAUNCHER,
                f"http://127.0.0.1:{server.server_port}/v5/market/kline",
                "--once",
                *options,
            ],
            directory=directory,
            environment=environment,
        )
    )


def saved_rows(directory: Path) -> list[tuple[object, ...]]:
    """Read real stored values from the default CLI database.

    Returns:
        Symbol, interval, timestamp and close for each persisted candle.

    """
    with closing(sqlite3.connect(directory / "data/candles.sqlite")) as db:
        return cast(
            "list[tuple[object, ...]]",
            db.execute(
                "SELECT symbol, interval, open_time_ms, close FROM candles "
                "ORDER BY open_time_ms"
            ).fetchall(),
        )


@pytest.mark.e2e
def test_cli_restart_backfill_and_overlap(
    exchange: ExchangeServer, tmp_path: Path
) -> None:
    """Persist default candles, exclude open candles and resume on restart."""
    boundary = time_ns() // 1_000_000 // MINUTE_MS * MINUTE_MS
    first, second = boundary - 10 * MINUTE_MS, boundary - 9 * MINUTE_MS
    exchange.state.include_open = True
    exchange.state.rows = [candle(first), candle(second)]
    initial = launch(exchange, tmp_path)
    if initial.returncode or "Saved 2 completed candles" not in initial.stderr:
        pytest.fail(f"Initial collection failed: {initial}")
    expected = [("BTCUSDT", "1m", first, 11.0), ("BTCUSDT", "1m", second, 11.0)]
    if saved_rows(tmp_path) != expected:
        pytest.fail("Incorrect default series or incomplete candle persisted")
    request = exchange.state.requests[0]
    if (
        request["symbol"] != ["BTCUSDT"]
        or request["category"] != ["linear"]
        or request["interval"] != ["1"]
        or int(request["end"][0]) - int(request["start"][0]) != 7 * 86_400_000
    ):
        pytest.fail(
            "Incorrect default exchange request or initial history window"
        )
    new = boundary - 8 * MINUTE_MS
    exchange.state.rows = [
        candle(second, close="12"),
        candle(new),
    ]
    exchange.state.requests.clear()
    restarted = launch(exchange, tmp_path)
    expected = [
        ("BTCUSDT", "1m", first, 11.0),
        ("BTCUSDT", "1m", second, 12.0),
        ("BTCUSDT", "1m", new, 11.0),
    ]
    if restarted.returncode or saved_rows(tmp_path) != expected:
        pytest.fail(
            f"Restart failed to backfill and refresh overlap: {restarted}"
        )
    if exchange.state.requests[0]["start"] != [str(second)]:
        pytest.fail("Restart must start at the latest persisted candle")
    if set(tmp_path.iterdir()) != {tmp_path / "data"}:
        pytest.fail("Data-only mode created unexpected account/config files")


@pytest.mark.e2e
@pytest.mark.parametrize("failure", ["http", "api"])
def test_cli_failure_preserves_archive_and_recovers(
    exchange: ExchangeServer, tmp_path: Path, failure: str
) -> None:
    """Return nonzero for HTTP/API failures and recover in a fresh process."""
    timestamp = time_ns() // 1_000_000 - 10 * MINUTE_MS
    exchange.state.rows = [candle(timestamp)]
    if launch(exchange, tmp_path).returncode:
        pytest.fail("Initial archive creation failed")
    before = saved_rows(tmp_path)
    if failure == "http":
        exchange.state.status = 503
    else:
        exchange.state.code = 10001
    failed = launch(exchange, tmp_path)
    if (
        failed.returncode != 1
        or "Candle collection failed" not in failed.stderr
    ):
        pytest.fail(f"CLI must signal exchange failure: {failed}")
    if saved_rows(tmp_path) != before:
        pytest.fail("Failed fetch changed persisted history")
    exchange.state.status, exchange.state.code = 200, 0
    exchange.state.rows = [candle(timestamp, close="12")]
    recovered = launch(exchange, tmp_path)
    if recovered.returncode or saved_rows(tmp_path) != [
        ("BTCUSDT", "1m", timestamp, 12.0)
    ]:
        pytest.fail(
            f"Fresh process did not recover after exchange failure: {recovered}"
        )


@pytest.mark.e2e
def test_cli_custom_series_and_empty_response(
    exchange: ExchangeServer, tmp_path: Path
) -> None:
    """Persist custom series and preserve history when the exchange is empty."""
    timestamp = time_ns() // 1_000_000 - 10 * MINUTE_MS
    exchange.state.rows = [candle(timestamp)]
    options = ("--symbol", "ethusdt", "--interval", "5m")
    result = launch(exchange, tmp_path, options=options)
    expected = [("ETHUSDT", "5m", timestamp, 11.0)]
    if result.returncode or saved_rows(tmp_path) != expected:
        pytest.fail(f"Custom series was not persisted correctly: {result}")
    if exchange.state.requests[0]["interval"] != ["5"]:
        pytest.fail("Expected 5-minute Bybit API interval")
    exchange.state.rows.clear()
    empty = launch(exchange, tmp_path, options=options)
    if empty.returncode or saved_rows(tmp_path) != expected:
        pytest.fail(
            "Empty response must preserve history and exit successfully"
        )


@pytest.mark.e2e
def test_cli_invalid_arguments_have_no_side_effects(
    exchange: ExchangeServer, tmp_path: Path
) -> None:
    """Reject invalid arguments before networking or creating persistence."""
    result = launch(exchange, tmp_path, options=("--poll-seconds", "0"))
    usage_error = 2
    if (
        result.returncode != usage_error
        or "must be positive" not in result.stderr
    ):
        pytest.fail(f"Expected argparse validation failure: {result}")
    if exchange.state.requests or list(tmp_path.iterdir()):
        pytest.fail("Invalid settings caused external side effects")


@pytest.mark.e2e
def test_installer_cli_data_only_lifecycle(tmp_path: Path) -> None:
    """Install and uninstall through real CLI with systemd commands recorded."""
    project = tmp_path / "application"
    project.mkdir()
    _ = (project / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    commands = tmp_path / "bin"
    commands.mkdir()
    log = tmp_path / "systemd.jsonl"
    # Debian containers have no user systemd manager. Only that OS boundary is
    # substituted; argument parsing, validation and unit writes are production.
    script = (
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['SYSTEMD_TEST_LOG'], 'a') as output:\n"
        "    output.write(json.dumps(sys.argv) + '\\n')\n"
    )
    for name in ("systemctl", "systemd-analyze"):
        executable = commands / name
        _ = executable.write_text(script)
        executable.chmod(0o755)
    environment = dict(os.environ)
    environment["HOME"] = str(tmp_path / "home")
    environment["PATH"] = str(commands) + os.pathsep + os.environ["PATH"]
    environment["SYSTEMD_TEST_LOG"] = str(log)
    base = [
        sys.executable,
        str(PROJECT.parent / "install/bot_services.py"),
        "--project",
        str(project),
        "--uv",
        sys.executable,
    ]
    installed = asyncio.run(
        run_process(
            [*base, "install", "--entity", "data-saver"],
            directory=tmp_path,
            environment=environment,
        )
    )
    unit_dir = tmp_path / "home/.config/systemd/user"
    unit = unit_dir / "paper-data-saver.service"
    if installed.returncode or not unit.is_file():
        pytest.fail(f"Standalone install failed: {installed}")
    if set(unit_dir.iterdir()) != {unit}:
        pytest.fail("Installer created additional trading/SMTP services")
    if "python -m server_bot.data_saver" not in unit.read_text():
        pytest.fail("Installed service does not invoke the data saver")
    recorded: object = [
        json.loads(line) for line in log.read_text().splitlines()
    ]
    expected = [
        [str(commands / "systemd-analyze"), "--user", "verify", str(unit)],
        [str(commands / "systemctl"), "--user", "daemon-reload"],
        [str(commands / "systemctl"), "--user", "enable", unit.name],
        [str(commands / "systemctl"), "--user", "start", unit.name],
    ]
    if recorded != expected:
        pytest.fail(f"Unexpected install commands: {recorded}")
    removed = asyncio.run(
        run_process(
            [*base, "uninstall", "--entity", "data-saver"],
            directory=tmp_path,
            environment=environment,
        )
    )
    if removed.returncode or unit.exists():
        pytest.fail(f"Standalone uninstall failed: {removed}")
    expected.extend([
        [str(commands / "systemctl"), "--user", "disable", "--now", unit.name],
        [str(commands / "systemctl"), "--user", "daemon-reload"],
    ])
    recorded = [json.loads(line) for line in log.read_text().splitlines()]
    if recorded != expected:
        pytest.fail(f"Unexpected uninstall commands: {recorded}")
