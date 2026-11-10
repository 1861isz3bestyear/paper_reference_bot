# Copyright 2026 anchored-vwam-backtest contributors
"""Collect HTTP inputs, transfer a backup, and load local durable evidence."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import pytest
from market_storage.archive import Archive
from market_storage.inputs import load_inputs
from market_storage.models import Source, Window

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.e2e


class Exchange(ThreadingHTTPServer):
    """Serve complete deterministic source schemas at real HTTP boundaries."""

    calls: list[tuple[str, dict[str, list[str]]]]

    def __init__(self) -> None:
        """Bind only the test's private loopback listener."""
        self.calls = []
        super().__init__(("127.0.0.1", 0), Handler)


class Handler(BaseHTTPRequestHandler):
    """Return source-specific JSON without contacting public exchanges."""

    def do_GET(self) -> None:
        """Record query parameters and return the requested source schema.

        Raises:
            TypeError: If the handler is attached to an unexpected server.

        """
        url = urlparse(self.path)
        if not isinstance(self.server, Exchange):
            msg = "Expected a fixture exchange server."
            raise TypeError(msg)
        self.server.calls.append((url.path, parse_qs(url.query)))
        payload = json.dumps(exchange_payload(url.path)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        _ = self.wfile.write(payload)


def exchange_payload(path: str) -> dict[str, object]:
    """Create exchange envelopes at the current five-minute source boundary.

    Returns:
        A complete public API response for the endpoint.

    Raises:
        ValueError: If production requests an unexpected endpoint.

    """
    now = datetime.now(UTC)
    millis = int(now.timestamp() // 300) * 300_000
    instrument = {
        "instrument_name": "BTC-TEST-C",
        "kind": "option",
        "is_active": True,
        "base_currency": "BTC",
        "option_type": "call",
        "contract_size": 1,
        "expiration_timestamp": int(
            (now + timedelta(days=10)).timestamp() * 1000
        ),
        "strike": 100000,
    }
    responses: dict[str, dict[str, object]] = {
        "get_instruments": {"result": [instrument]},
        "get_book_summary_by_currency": {
            "result": [{
                "instrument_name": "BTC-TEST-C",
                "open_interest": 20,
                "mark_iv": 50,
            }]
        },
        "get_index_price": {"result": {"index_price": 100000}},
        "orderbook": {
            "retCode": 0,
            "result": {
                "s": "BTCUSDT",
                "ts": int(now.timestamp() * 1000),
                "b": [["99999", "2"]],
                "a": [["100001", "1"]],
            },
        },
        "tickers": {
            "retCode": 0,
            "result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "100000"}]},
        },
        "open-interest": {
            "retCode": 0,
            "result": {
                "symbol": "BTCUSDT",
                "list": [{"timestamp": str(millis), "openInterest": "1000"}],
            },
        },
        "margin-loan-growth": {
            "code": "00000",
            "data": [{"ts": str(millis), "growthRate": "0.2"}],
        },
        "margin-loans": {
            "code": "00000",
            "requestTime": millis,
            "data": {"dailyInterest": "0.001", "annualInterest": "0.365"},
        },
    }
    endpoint = path.rsplit("/", 1)[-1]
    if endpoint not in responses:
        msg = f"Unexpected exchange endpoint: {endpoint}"
        raise ValueError(msg)
    return responses[endpoint]


@pytest.fixture
def exchange() -> Iterator[Exchange]:
    """Serve real HTTP for one scenario and close all test-owned resources.

    Yields:
        The endpoint fixture with its observed requests.

    """
    with Exchange() as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server
        finally:
            server.shutdown()
            worker.join(timeout=5)


@dataclass(frozen=True, kw_only=True)
class ProcessResult:
    """Capture an isolated CLI's status and diagnostics."""

    returncode: int
    output: str


async def run_cli(module: str, arguments: tuple[str, ...]) -> ProcessResult:
    """Run the actual application parser and production subprocess.

    Returns:
        The exit status and combined CLI output.

    """
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        module,
        *arguments,
        cwd=ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    finally:
        if process.returncode is None:
            process.kill()
            _ = await process.wait()
    return ProcessResult(
        returncode=await process.wait(), output=output.decode()
    )


def collect_arguments(path: Path, exchange: Exchange) -> tuple[str, ...]:
    """Redirect exchange addresses while retaining the actual collector CLI.

    Returns:
        CLI options for one persisted collection.

    """
    base = f"http://127.0.0.1:{exchange.server_port}/"
    return (
        "--once",
        "--output",
        str(path),
        "--bybit-url",
        base + "bybit/",
        "--bitget-url",
        base + "bitget/",
        "--deribit-url",
        base + "deribit/",
    )


def test_collector_restarts_without_duplicate_successful_buckets(
    tmp_path: Path, exchange: Exchange
) -> None:
    """Persist schedules across separate collector processes with real HTTP."""
    archive = tmp_path / "archive.sqlite"
    arguments = collect_arguments(archive, exchange)
    for _ in range(2):
        result = asyncio.run(run_cli("server_bot.tunnel_archive", arguments))
        assert result.returncode == 0, result.output
        assert '"failed_sources": 0' in result.output
    window = Window(
        asset="BTC",
        start=datetime.now(UTC) - timedelta(hours=1),
        end=datetime.now(UTC),
    )
    rows = Archive(path=archive).load(window)
    buckets = {
        (row.source, int(row.requested.timestamp()) // row.source.interval)
        for row in rows
    }
    assert len(buckets) == len(rows)
    assert {row.source for row in rows} == set(Source)
    assert not any("kline" in path for path, _ in exchange.calls)
    assert all(
        params["intervalTime"] == ["5min"]
        for path, params in exchange.calls
        if path.endswith("open-interest")
    )


def test_collection_to_vps_export_to_local_calculation_inputs(
    tmp_path: Path, exchange: Exchange, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Collect, commit, download a live backup, and load causal GEX inputs."""
    remote = tmp_path / "remote deployment"
    (remote / "storage").parent.mkdir()
    (remote / "storage").symlink_to(
        ROOT.parent / "storage", target_is_directory=True
    )
    archive = remote / "server_bot/tunnel_inputs.sqlite"
    collected = asyncio.run(
        run_cli(
            "server_bot.tunnel_archive", collect_arguments(archive, exchange)
        )
    )
    assert collected.returncode == 0, collected.output
    ssh = tmp_path / "ssh"
    _ = ssh.write_text(
        f"#!{sys.executable}\nimport os, sys\nos.execv('/bin/sh', ['sh', '-c',"
        " sys.argv[-1]])\n"
    )
    ssh.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    local = tmp_path / "local.sqlite"
    downloaded = asyncio.run(
        run_cli(
            "market_storage",
            (
                "pull",
                "--host",
                "fixture-vps",
                "--remote-root",
                str(remote),
                "--output",
                str(local),
            ),
        )
    )
    assert downloaded.returncode == 0, downloaded.output
    window = Window(
        asset="BTC",
        start=datetime.now(UTC) - timedelta(hours=1),
        end=datetime.now(UTC),
    )
    inputs = load_inputs(Archive(path=local), window)
    assert len(inputs.gex) == 1
    assert inputs.stream(Source.OI)
    assert inputs.stream(Source.GROWTH_HOURLY)
    assert inputs.gex[0].available <= window.end
    assert json.loads(inputs.gex[0].spot.payload) == {
        "result": {"index_price": 100000}
    }
