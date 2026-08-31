"""Opt-in tests for services already installed in a Linux user systemd manager."""

from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path

import pytest

import bot_services

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_SYSTEMD_INTEGRATION") != "1",
    reason="set RUN_SYSTEMD_INTEGRATION=1 after installing the services",
)


def command(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, text=True, capture_output=True)


@pytest.fixture(scope="module")
def layout() -> bot_services.Layout:
    resolved = bot_services.resolve_layout()
    bot_services.validate_layout(resolved)
    return resolved


def test_generated_units_are_valid_and_use_resume(layout: bot_services.Layout) -> None:
    unit_paths = [layout.unit_dir / name for name in bot_services.SERVICE_NAMES]
    assert all(path.is_file() for path in unit_paths)
    command("systemd-analyze", "--user", "verify", *(str(path) for path in unit_paths))

    expected = {
        "paper-bot-reference.service": "reference_bot.cli run --resume",
        "paper-bot-live.service": "live_paper_bot.cli run --resume",
        "bybit-demo.service": "run-bybit-demo --resume",
    }
    for service, marker in expected.items():
        unit = command("systemctl", "--user", "cat", service).stdout
        assert marker in unit
        assert str(layout.project) in unit
        assert "Restart=on-failure" in unit
        assert "KillSignal=SIGINT" in unit


def test_all_services_are_enabled_active_and_unique() -> None:
    for state in ("is-enabled", "is-active"):
        result = command("systemctl", "--user", state, *bot_services.SERVICE_NAMES)
        assert all(line.strip() in {"enabled", "active"} for line in result.stdout.splitlines())
    assert bot_services.check_services()


def test_demo_service_restarts_and_writes_to_journal() -> None:
    before = command(
        "systemctl", "--user", "show", "bybit-demo.service", "--property=MainPID", "--value"
    ).stdout.strip()
    command("systemctl", "--user", "restart", "bybit-demo.service")
    assert command("systemctl", "--user", "is-active", "bybit-demo.service").stdout.strip() == "active"
    after = command(
        "systemctl", "--user", "show", "bybit-demo.service", "--property=MainPID", "--value"
    ).stdout.strip()
    assert after not in {"", "0"}
    assert after != before
    journal = command(
        "journalctl", "--user-unit", "bybit-demo.service", "--since", "5 minutes ago", "--no-pager"
    ).stdout
    assert "Bybit DEMO executor started" in journal


def test_diagnostic_archive_contains_every_service(layout: bot_services.Layout, tmp_path: Path) -> None:
    archive = bot_services.collect_logs(layout, tmp_path, since="1 hour ago")
    assert archive.is_file()
    with tarfile.open(archive, "r:gz") as bundle:
        names = bundle.getnames()
    for service in bot_services.SERVICE_NAMES:
        assert any(name.endswith(f"/{service}.log") for name in names)
    assert any(name.endswith("/services-status.txt") for name in names)
    assert any(name.endswith("/bot-status.txt") for name in names)
    assert any(name.endswith("/bot-stats.txt") for name in names)
