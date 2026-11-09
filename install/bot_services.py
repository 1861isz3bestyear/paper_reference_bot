#!/usr/bin/env python3
# Copyright 2026 avwap-workspace-tests contributors
"""Install and diagnose selectable user systemd bot services."""

from __future__ import annotations

import argparse
import asyncio
import re
import shutil
import sys
import tarfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ENTITY_SERVICES = {
    "data-saver": "paper-data-saver.service",
    "gex": "paper-gex-archive.service",
    "reference": "paper-bot-reference.service",
    "paper": "paper-bot-live.service",
    "demo": "bybit-demo.service",
    "bybit": "bybit-mainnet.service",
    "smtp": "smtp-monitor.timer",
}
ENTITY_MARKERS = {
    "data-saver": "server_bot.data_saver",
    "gex": "server_bot.tunnel_archive",
    "reference": "reference_bot.cli",
    "paper": "live_paper_bot.cli",
    "demo": "run-bybit-demo",
    "bybit": "run-bybit-mainnet",
}
ENTITY_NAMES = tuple(ENTITY_SERVICES)
DEFAULT_ENTITY_NAMES = ("reference", "paper", "demo", "smtp")
SERVICE_NAMES = (*tuple(ENTITY_SERVICES.values()), "smtp-monitor.service")
SECRET_PATTERN = re.compile(
    r"(?im)^([^\n=]*(?:api[_"
    r" -]?(?:key|secret)|password|authorization|bearer)[^\n=]*=)[^\n]*$"
)


@dataclass(frozen=True)
class Layout:
    """Filesystem locations used to generate and manage service units."""

    project: Path
    uv: Path
    config: Path
    paper_env: Path
    demo_env: Path
    bybit_env: Path
    unit_dir: Path
    monitor_dir: Path | None = None

    @property
    def monitor_root(self) -> Path:
        """The configured monitor directory or its sibling default.

        Returns:
            The directory containing the health monitor script.

        """
        return self.monitor_dir or self.project.parent / "smtp_monitor"

    @property
    def monitor_env(self) -> Path:
        """The monitor credential file path.

        Returns:
            The SMTP environment file inside the monitor directory.

        """
        return self.monitor_root / "smtp_monitor.env"


@dataclass(frozen=True, kw_only=True)
class CommandResult:
    """Exit status and captured command output."""

    args: Sequence[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[..., CommandResult]


def resolve_layout(
    project: Path | None = None, uv: Path | None = None
) -> Layout:
    """Resolve installation paths and find the uv executable.

    Returns:
        Absolute paths for the selected project and its service files.

    Raises:
        RuntimeError: If uv cannot be found.

    """
    root = (
        project or Path(__file__).resolve().parents[1] / "server_bot"
    ).resolve()
    uv_value = uv or (Path(found) if (found := shutil.which("uv")) else None)
    if uv_value is None:
        msg = "uv is not installed or is not available in PATH"
        raise RuntimeError(msg)
    return Layout(
        project=root,
        uv=uv_value.resolve(),
        config=root / "paper_bot_config.json",
        paper_env=root / "bybitapi.env",
        demo_env=(
            root / "bybitdemoapi.env"
            if (root / "bybitdemoapi.env").is_file()
            else root / "bybitapidemo.env"
        ),
        bybit_env=root / "bybitrealapi.env",
        unit_dir=Path.home() / ".config/systemd/user",
        monitor_dir=root.parent / "smtp_monitor",
    )


def selected_entities(entities: Sequence[str] | None = None) -> tuple[str, ...]:
    """Validate and deduplicate selected service entities.

    Returns:
        Selected entities in their original order, or the default selection.

    Raises:
        ValueError: If any selected entity is unknown.

    """
    selected = tuple(entities or DEFAULT_ENTITY_NAMES)
    unknown = sorted(set(selected) - set(ENTITY_NAMES))
    if unknown:
        raise ValueError("Unknown service entities: " + ", ".join(unknown))
    return tuple(dict.fromkeys(selected))


def service_names(entities: Sequence[str] | None = None) -> tuple[str, ...]:
    """Map selected entities to their systemd service or timer names.

    Returns:
        Unit names used to control the selected entities.

    """
    return tuple(ENTITY_SERVICES[name] for name in selected_entities(entities))


def unit_names(entities: Sequence[str] | None = None) -> tuple[str, ...]:
    """Include supporting units needed by the selected entities.

    Returns:
        Service and timer files to install or remove.

    """
    names = list(service_names(entities))
    if "smtp" in selected_entities(entities):
        names.insert(names.index("smtp-monitor.timer"), "smtp-monitor.service")
    return tuple(names)


def validate_layout(
    layout: Layout, entities: Sequence[str] | None = None
) -> None:
    """Check required files and restrict credential access to the owner.

    Raises:
        RuntimeError: If a required file is missing or credentials are exposed.

    """
    selected = selected_entities(entities)
    required = [layout.project / "pyproject.toml"]
    if set(selected) - {"data-saver", "gex"}:
        required.append(layout.config)
    credential_paths: list[Path] = []
    if {"reference", "paper"} & set(selected):
        credential_paths.append(layout.paper_env)
    if "demo" in selected:
        credential_paths.append(layout.demo_env)
    if "bybit" in selected:
        credential_paths.append(layout.bybit_env)
    if "smtp" in selected:
        required.append(layout.monitor_root / "monitor.py")
        credential_paths.append(layout.monitor_env)
    required.extend(credential_paths)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("Missing required file(s): " + ", ".join(missing))
    insecure = [
        str(path)
        for path in credential_paths
        if path.is_file() and path.stat().st_mode & 0o077
    ]
    if insecure:
        raise RuntimeError(
            "Credential file permissions must be 600: " + ", ".join(insecure)
        )


def _quote(value: Path) -> str:
    return '"' + str(value).replace("%", "%%").replace('"', '\\"') + '"'


def unit_contents(layout: Layout) -> dict[str, str]:
    """Render all supported systemd service and timer definitions.

    Returns:
        Unit filenames mapped to their complete contents.

    """
    common = (
        "After=network-online.target\nWants=network-online.target\n\n"
        "[Service]\nType=simple\n"
        f"WorkingDirectory={layout.project}\n"
        "Environment=PYTHONUNBUFFERED=1\n"
    )
    suffix = (
        "Restart=on-failure\nRestartSec=15\nKillSignal=SIGINT\nTimeoutStopSec=30\n\n"
        "[Install]\nWantedBy=default.target\n"
    )
    commands = {
        "paper-gex-archive.service": (
            "GEX raw input archive",
            (
                f"{_quote(layout.uv)} run python -m server_bot.tunnel_archive"
                " --asset BTC"
            ),
        ),
        "paper-data-saver.service": (
            "Public candle data saver",
            f"{_quote(layout.uv)} run python -m server_bot.data_saver",
        ),
        "paper-bot-reference.service": (
            "Reference paper bot",
            (
                f"{_quote(layout.uv)} run python -m reference_bot.cli run"
                f" --resume --config {_quote(layout.config)}"
            ),
        ),
        "paper-bot-live.service": (
            "Live paper bot",
            (
                f"{_quote(layout.uv)} run python -m live_paper_bot.cli run"
                f" --resume --config {_quote(layout.config)}"
            ),
        ),
        "bybit-demo.service": (
            "Bybit Demo bot",
            (
                f"{_quote(layout.uv)} run python -m server_bot.cli"
                f" run-bybit-demo --resume --config {_quote(layout.config)}"
                f" --env {_quote(layout.demo_env)}"
            ),
        ),
        "bybit-mainnet.service": (
            "Bybit mainnet bot (REAL FUNDS)",
            (
                f"{_quote(layout.uv)} run python -m server_bot.cli"
                " run-bybit-mainnet --confirm-live --resume --config"
                f" {_quote(layout.config)} --env {_quote(layout.bybit_env)}"
            ),
        ),
    }
    units = {
        name: (
            f"[Unit]\nDescription={description}\n{common}ExecStart={command}\n{suffix}"
        )
        for name, (description, command) in commands.items()
    }
    monitor_command = (
        f"{_quote(layout.uv)} run --project {_quote(layout.project)} python"
        f" {_quote(layout.monitor_root / 'monitor.py')} --project"
        f" {_quote(layout.project)} --env {_quote(layout.monitor_env)} --state"
        f" {_quote(layout.monitor_root / 'alert_state.json')}"
    )
    units["smtp-monitor.service"] = (
        "[Unit]\nDescription=SMTP health monitor for reference and Bybit Demo"
        " bots\n"
        "After=network-online.target\nWants=network-online.target\n\n"
        "[Service]\nType=oneshot\n"
        f"WorkingDirectory={layout.project}\nExecStart={monitor_command}\n"
    )
    units["smtp-monitor.timer"] = (
        "[Unit]\nDescription=Run the SMTP bot monitor every minute\n\n"
        "[Timer]\nOnBootSec=15sec\nOnUnitActiveSec=1min\nPersistent=true\nRandomizedDelaySec=10\n"
        "Unit=smtp-monitor.service\n\n[Install]\nWantedBy=timers.target\n"
    )
    return units


async def _execute_process(
    command: Sequence[str], *, capture_output: bool
) -> CommandResult:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE if capture_output else None,
        stderr=asyncio.subprocess.PIPE if capture_output else None,
    )
    stdout, stderr = await process.communicate()
    return CommandResult(
        args=command,
        returncode=await process.wait(),
        stdout=stdout.decode(errors="replace") if stdout else "",
        stderr=stderr.decode(errors="replace") if stderr else "",
    )


def run(
    command: Sequence[str], *, check: bool = True, capture_output: bool = False
) -> CommandResult:
    """Execute an argument vector directly, with optional output capture.

    Returns:
        The process status and captured text.

    Raises:
        RuntimeError: If a checked command fails.

    """
    result = asyncio.run(
        _execute_process(command, capture_output=capture_output)
    )
    if check and result.returncode:
        message = (
            f"Command {command[0]} failed with exit code {result.returncode}:"
            f" {result.stderr}"
        )
        raise RuntimeError(message)
    return result


def install(
    layout: Layout,
    runner: Runner = run,
    *,
    start: bool = True,
    entities: Sequence[str] | None = None,
) -> None:
    """Write, verify, and enable selected units, optionally starting them."""
    selected = selected_entities(entities)
    names = service_names(selected)
    files = unit_names(selected)
    validate_layout(layout, selected)
    layout.unit_dir.mkdir(parents=True, exist_ok=True)
    units = unit_contents(layout)
    for name in files:
        contents = units[name]
        _ = (layout.unit_dir / name).write_text(contents, encoding="utf-8")
    _ = runner([
        "systemd-analyze",
        "--user",
        "verify",
        *(str(layout.unit_dir / n) for n in files),
    ])
    _ = runner(["systemctl", "--user", "daemon-reload"])
    _ = runner(["systemctl", "--user", "enable", *names])
    if start:
        _ = runner(["systemctl", "--user", "start", *names])


def services_action(
    action: str, runner: Runner = run, entities: Sequence[str] | None = None
) -> None:
    """Apply a systemctl action to the selected units."""
    _ = runner(["systemctl", "--user", action, *service_names(entities)])


def uninstall(
    layout: Layout, runner: Runner = run, entities: Sequence[str] | None = None
) -> None:
    """Disable selected units and remove their generated definitions."""
    names = service_names(entities)
    _ = runner(["systemctl", "--user", "disable", "--now", *names], check=False)
    for name in unit_names(entities):
        (layout.unit_dir / name).unlink(missing_ok=True)
    _ = runner(["systemctl", "--user", "daemon-reload"])


def check_services(
    runner: Runner = run, entities: Sequence[str] | None = None
) -> bool:
    """Check that selected units are enabled, active, and unique.

    Returns:
        Whether all service state and process count checks succeeded.

    """
    selected = selected_entities(entities)
    names = service_names(selected)
    good = True
    for state in ("is-enabled", "is-active"):
        result = runner(
            ["systemctl", "--user", state, *names],
            check=False,
            capture_output=True,
        )
        if result.returncode:
            good = False
            output = (result.stdout + result.stderr).strip()
            if output:
                _ = sys.stdout.write(output + "\n")
    processes = runner(
        [
            "pgrep",
            "-af",
            "|".join(
                ENTITY_MARKERS[name]
                for name in selected
                if name in ENTITY_MARKERS
            ),
        ],
        check=False,
        capture_output=True,
    )
    lines = [
        line
        for line in processes.stdout.splitlines()
        if line.strip() and "uv run " not in line
    ]
    for marker in (
        ENTITY_MARKERS[name] for name in selected if name in ENTITY_MARKERS
    ):
        count = sum(marker in line for line in lines)
        if count != 1:
            _ = sys.stdout.write(
                f"Expected exactly one {marker} process, found {count}.\n"
            )
            good = False
    return good


def redact(text: str) -> str:
    """Remove credential values from captured diagnostic text.

    Returns:
        Text with secret assignment values replaced by a redaction marker.

    """
    return SECRET_PATTERN.sub(r"\1<redacted>", text)


def _capture(runner: Runner, command: Sequence[str]) -> str:
    result = runner(command, check=False, capture_output=True)
    return redact(result.stdout + result.stderr)


def collect_logs(
    layout: Layout,
    output_dir: Path,
    runner: Runner = run,
    *,
    since: str = "7 days ago",
    entities: Sequence[str] | None = None,
) -> Path:
    """Archive redacted journals and status reports for selected services.

    Returns:
        The path to the compressed diagnostics archive.

    """
    names = unit_names(entities)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = output_dir / f"bot-diagnostics-{stamp}"
    report.mkdir(parents=True, exist_ok=False)
    for service in names:
        contents = _capture(
            runner,
            [
                "journalctl",
                "--user-unit",
                service,
                "--since",
                since,
                "--no-pager",
                "-o",
                "short-iso-precise",
            ],
        )
        _ = (report / f"{service}.log").write_text(contents, encoding="utf-8")
    commands = {
        "services-status.txt": [
            "systemctl",
            "--user",
            "status",
            *names,
            "--no-pager",
        ],
        "bot-status.txt": [
            str(layout.uv),
            "run",
            "--project",
            str(layout.project),
            "python",
            "-m",
            "server_bot.cli",
            "status",
            "--config",
            str(layout.config),
        ],
        "bot-stats.txt": [
            str(layout.uv),
            "run",
            "--project",
            str(layout.project),
            "python",
            "-m",
            "server_bot.cli",
            "stats",
            "--config",
            str(layout.config),
        ],
    }
    if set(selected_entities(entities)) <= {"data-saver", "gex"}:
        _ = commands.pop("bot-status.txt")
        _ = commands.pop("bot-stats.txt")
    for filename, command in commands.items():
        _ = (report / filename).write_text(
            _capture(runner, command), encoding="utf-8"
        )
    archive = output_dir / f"{report.name}.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(report, arcname=report.name)
    return archive


class Arguments(argparse.Namespace):
    """Typed values populated by argparse's path, flag and choice validators."""

    project: Path | None = None
    uv: Path | None = None
    command: str = ""
    no_start: bool = False
    entities: list[str] | None = None
    since: str = "7 days ago"
    output_dir: Path

    def __init__(self) -> None:
        """Capture path defaults at parsing time, matching argparse behavior."""
        super().__init__()
        self.output_dir = Path.cwd()


def parse_args(argv: Sequence[str] | None = None) -> Arguments:
    """Parse installer commands and validated path and entity options.

    Returns:
        The typed command options.

    """
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        "--project", type=Path, help="server_bot project directory"
    )
    _ = parser.add_argument("--uv", type=Path, help="absolute path to uv")
    commands = parser.add_subparsers(dest="command", required=True)
    install_parser = commands.add_parser("install")
    _ = install_parser.add_argument("--no-start", action="store_true")
    _ = install_parser.add_argument(
        "--entity", action="append", choices=ENTITY_NAMES, dest="entities"
    )
    for command in ("start", "stop", "restart", "status", "check", "uninstall"):
        subparser = commands.add_parser(command)
        _ = subparser.add_argument(
            "--entity", action="append", choices=ENTITY_NAMES, dest="entities"
        )
    logs = commands.add_parser("collect-logs")
    _ = logs.add_argument("--since", default="7 days ago")
    _ = logs.add_argument("--output-dir", type=Path, default=Path.cwd())
    _ = logs.add_argument(
        "--entity", action="append", choices=ENTITY_NAMES, dest="entities"
    )
    return parser.parse_args(argv, namespace=Arguments())


def execute_command(args: Arguments, layout: Layout) -> int:
    """Dispatch parsed options to the selected installation operation.

    Returns:
        Zero on success, or one if service checks fail.

    """
    if args.command == "install":
        install(layout, start=not args.no_start, entities=args.entities)
    elif args.command in {"start", "stop", "restart", "status"}:
        services_action(args.command, entities=args.entities)
    elif args.command == "check":
        return 0 if check_services(entities=args.entities) else 1
    elif args.command == "uninstall":
        uninstall(layout, entities=args.entities)
    else:
        archive = collect_logs(
            layout,
            args.output_dir.resolve(),
            since=args.since,
            entities=args.entities,
        )
        _ = sys.stdout.write(f"{archive}\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Execute an installer command and report operational failures.

    Returns:
        Zero on success, or one on a failed check or command.

    """
    args = parse_args(argv)
    try:
        layout = resolve_layout(args.project, args.uv)
        return execute_command(args, layout)
    except (OSError, RuntimeError) as exc:
        _ = sys.stderr.write(f"Error: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
