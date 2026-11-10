# Copyright 2026 avwap-workspace-tests contributors
"""Exercise generated service commands against the real application parsers."""

import asyncio
import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from . import bot_services as services

pytestmark = pytest.mark.integration
USAGE_ERROR = 2
PROJECT = Path(__file__).resolve().parents[1] / "server_bot"


@dataclass(frozen=True, kw_only=True)
class CommandResult:
    """Keep the process exit status and diagnostics together."""

    returncode: int
    output: str


@pytest.fixture
def deployment(tmp_path: Path) -> services.Layout:
    """Create a credential-free deployment using the installed application.

    Returns:
        An isolated layout with real application code and dependencies.

    """
    project = tmp_path / "application with spaces"
    project.mkdir()
    for name in (
        "pyproject.toml",
        "uv.lock",
        ".venv",
        "server_bot",
        "shared",
        "reference_bot",
        "live_paper_bot",
        "bybit_demo_bot",
        "bybit_bot",
    ):
        (project / name).symlink_to(PROJECT / name)
    uv = shutil.which("uv")
    assert uv is not None
    return services.Layout(
        project=project,
        uv=Path(uv),
        config=project / "missing-config.json",
        paper_env=project / "missing-paper.env",
        demo_env=project / "missing-demo.env",
        bybit_env=project / "missing-mainnet.env",
        unit_dir=tmp_path / "units",
    )


async def execute_unit(
    unit: str, *, arguments: tuple[str, ...]
) -> CommandResult:
    """Execute the rendered command in its declared working directory.

    Returns:
        Exit status and combined diagnostic output.

    """
    fields = dict(
        line.split("=", 1) for line in unit.splitlines() if "=" in line
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not any(word in key.upper() for word in ("BYBIT", "SMTP", "API_KEY"))
    }
    environment.update(UV_NO_SYNC="1", UV_OFFLINE="1")
    process = await asyncio.create_subprocess_exec(
        *shlex.split(fields["ExecStart"]),
        *arguments,
        cwd=fields["WorkingDirectory"],
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    finally:
        if process.returncode is None:
            process.kill()
            _ = await process.wait()
    return CommandResult(
        returncode=await process.wait(), output=output.decode()
    )


@pytest.mark.parametrize(
    "entity", ["data-saver", "gex", "reference", "paper", "demo", "bybit"]
)
def test_generated_command_matches_application(
    deployment: services.Layout, entity: str
) -> None:
    """Require every generated bot command to reach its actual CLI parser."""
    unit = services.unit_contents(deployment)[services.ENTITY_SERVICES[entity]]
    before = set(deployment.project.iterdir())
    result = asyncio.run(execute_unit(unit, arguments=("--help",)))
    assert result.returncode == 0, result.output
    assert "usage:" in result.output
    assert set(deployment.project.iterdir()) == before


@pytest.mark.parametrize("entity", ["data-saver", "gex"])
def test_collector_command_rejects_invalid_settings_without_storage(
    deployment: services.Layout, entity: str
) -> None:
    """Validate generated collector arguments before creating any archive."""
    services.validate_layout(deployment, entities=(entity,))
    unit = services.unit_contents(deployment)[services.ENTITY_SERVICES[entity]]
    before = set(deployment.project.iterdir())
    result = asyncio.run(
        execute_unit(unit, arguments=("--poll-seconds", "0", "--once"))
    )
    assert result.returncode == USAGE_ERROR, result.output
    assert "must be positive" in result.output
    assert set(deployment.project.iterdir()) == before


@pytest.mark.parametrize("entity", ["reference", "paper", "demo", "bybit"])
def test_trading_command_validates_configuration_before_starting(
    deployment: services.Layout, entity: str
) -> None:
    """Require generated trading options to parse before local file failure."""
    unit = services.unit_contents(deployment)[services.ENTITY_SERVICES[entity]]
    before = set(deployment.project.iterdir())
    result = asyncio.run(execute_unit(unit, arguments=()))
    assert result.returncode != 0, result.output
    assert "missing-config.json" in result.output, result.output
    assert "unrecognized arguments" not in result.output
    assert set(deployment.project.iterdir()) == before
