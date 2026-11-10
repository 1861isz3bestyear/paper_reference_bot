# Copyright 2026 anchored-vwam-backtest contributors
"""Run one shared tunnel archive poll alongside active bot processes."""

from __future__ import annotations

import fcntl
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from time import time
from typing import TYPE_CHECKING

import requests

from server_bot.tunnel_archive import collect_due as collect

if TYPE_CHECKING:
    from collections.abc import Generator

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_PATH = ROOT / "tunnel_inputs.sqlite"
LOGGER = logging.getLogger(__name__)
POLL_SECONDS = 300


def poll_archive(path: Path) -> None:
    """Serialize polls across bots and preserve cadence across restarts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        _ = lock.seek(0)
        previous = lock.read().strip()
        try:
            last_poll = float(previous) if previous else None
        except ValueError:
            LOGGER.warning("Ignoring invalid tunnel archive poll timestamp")
            last_poll = None
        if last_poll is not None and int(time() // POLL_SECONDS) == int(
            last_poll // POLL_SECONDS
        ):
            return
        started = time()
        with requests.Session() as session:
            failures = collect(path, session)
        _ = lock.seek(0)
        _ = lock.truncate()
        _ = lock.write(str(started))
        if failures:
            LOGGER.warning(
                "Tunnel archive poll: %s unavailable sources", failures
            )


def _run_archive(stop: threading.Event, path: Path) -> None:
    while not stop.is_set():
        try:
            poll_archive(path)
        except OSError, sqlite3.Error, ValueError:
            LOGGER.exception("Tunnel archive poll failed")
        _ = stop.wait(POLL_SECONDS - time() % POLL_SECONDS)


@contextmanager
def archive_while_running() -> Generator[None]:
    """Start collection without delaying trading; stop with the owning bot.

    Other running bots continue collection when this bot exits. A daemon
    thread ensures a slow exchange cannot keep a stopped bot process alive.

    """
    stop = threading.Event()
    worker = threading.Thread(
        target=_run_archive,
        args=(stop, ARCHIVE_PATH),
        name="tunnel-archive",
        daemon=True,
    )
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=1)
