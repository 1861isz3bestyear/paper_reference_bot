# Copyright 2026 anchored-vwam-backtest contributors
"""Exercise automatic archive startup, shutdown and shared polling."""

from __future__ import annotations

import fcntl
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from live_paper_bot import cli as live_cli
from reference_bot import cli as reference_cli
from server_bot import tunnel_runtime

if TYPE_CHECKING:
    from collections.abc import Generator

    import requests


def test_shared_poll_is_throttled_and_lock_is_respected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify shared poll is throttled and lock is respected."""
    calls: list[Path] = []

    def collect(path: Path, _session: requests.Session) -> int:
        """Collect."""
        calls.append(path)
        return 0

    monkeypatch.setattr(tunnel_runtime, "collect", collect)
    path = tmp_path / "archive.sqlite"
    with path.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        tunnel_runtime.poll_archive(path)
    if calls != []:
        pytest.fail("Expected calls == []")
    tunnel_runtime.poll_archive(path)
    tunnel_runtime.poll_archive(path)
    if calls != [path]:
        pytest.fail("Expected calls == [path]")
    _ = path.with_suffix(".lock").write_text("0", encoding="utf-8")
    tunnel_runtime.poll_archive(path)
    if calls != [path, path]:
        pytest.fail("Expected calls == [path, path]")


def test_worker_starts_immediately_and_stops_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify worker starts immediately and stops on exit."""
    polled = threading.Event()
    workers: list[threading.Thread] = []

    def poll(_path: Path) -> None:
        """Poll."""
        workers.append(threading.current_thread())
        polled.set()

    monkeypatch.setattr(tunnel_runtime, "poll_archive", poll)
    with tunnel_runtime.archive_while_running():
        if not (polled.wait(timeout=2)):
            pytest.fail("Expected polled.wait(timeout=2)")
    if len(workers) != 1:
        pytest.fail("Expected len(workers) == 1")
    if workers[0].is_alive():
        pytest.fail("Expected not workers[0].is_alive()")


@pytest.mark.parametrize("package", ["reference_bot", "live_paper_bot"])
def test_bot_run_owns_archive_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package: str
) -> None:
    """Verify bot run owns archive lifecycle."""
    cli = reference_cli if package == "reference_bot" else live_cli
    events: list[str] = []

    @contextmanager
    def archive() -> Generator[None]:
        """Archive."""
        events.append("archive-start")
        try:
            yield
        finally:
            events.append("archive-stop")

    class Bot:
        """Represent bot."""

        def __init__(self, _config: Path) -> None:
            """Initialize the test fixture."""
            events.append("bot-created")

        def run_forever(self) -> None:
            """Run forever."""
            events.append("bot-run")
            message = "bot stopped"
            raise RuntimeError(message)

    for name in ("INSTANCE_LOCK_FILE", "PID_FILE", "STATE_FILE"):
        monkeypatch.setattr(cli, name, tmp_path / name)
    monkeypatch.setattr(cli, "PaperBot", Bot)
    monkeypatch.setattr(cli, "archive_while_running", archive)
    with pytest.raises(RuntimeError, match="bot stopped"):
        cli.run_command(tmp_path / "config.json", resume=True)
    if events != ["bot-created", "archive-start", "bot-run", "archive-stop"]:
        pytest.fail(
            "Expected events == ['bot-created', 'archive-start', 'bot-run',"
            " 'archive-stop']"
        )
    if (tmp_path / "PID_FILE").exists():
        pytest.fail("Expected not (tmp_path / 'PID_FILE').exists()")


def test_corrupt_poll_timestamp_is_repaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A damaged cadence marker must not disable all future collection."""
    path = tmp_path / "archive.sqlite"
    lock = path.with_suffix(".lock")
    _ = lock.write_text("partial timestamp", encoding="utf-8")
    collected: list[Path] = []

    def collect(output: Path, _session: requests.Session) -> int:
        """Collect."""
        collected.append(output)
        return 0

    monkeypatch.setattr(tunnel_runtime, "collect", collect)
    monkeypatch.setattr(tunnel_runtime, "time", lambda: 1000.0)
    tunnel_runtime.poll_archive(path)
    tunnel_runtime.poll_archive(path)
    if collected != [path]:
        pytest.fail("Expected collected == [path]")
    if float(lock.read_text(encoding="utf-8")) != 1000.0:
        pytest.fail(
            "Expected float(lock.read_text(encoding='utf-8')) == 1000.0"
        )


def test_next_minute_is_polled_without_waiting_after_slow_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Request latency must not add another full minute to the cadence."""
    path = tmp_path / "archive.sqlite"
    clock = [120.0]
    calls: list[float] = []

    def collect(_path: Path, _session: requests.Session) -> int:
        """Simulate a slow poll.

        Returns:
            Zero failed sources.

        """
        calls.append(clock[0])
        clock[0] += 45
        return 0

    monkeypatch.setattr(tunnel_runtime, "time", lambda: clock[0])
    monkeypatch.setattr(tunnel_runtime, "collect", collect)
    tunnel_runtime.poll_archive(path)
    tunnel_runtime.poll_archive(path)
    clock[0] = 180
    tunnel_runtime.poll_archive(path)
    if calls != [120.0, 180]:
        pytest.fail("Poll once per UTC minute despite request latency")
