#!/usr/bin/env python3
"""Install, manage, verify, and diagnose selectable user systemd bot services."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

ENTITY_SERVICES = {
    "reference": "paper-bot-reference.service",
    "paper": "paper-bot-live.service",
    "demo": "bybit-demo.service",
    "bybit": "bybit-mainnet.service",
    "smtp": "smtp-monitor.timer",
}
ENTITY_MARKERS = {
    "reference": "reference_bot.cli",
    "paper": "live_paper_bot.cli",
    "demo": "run-bybit-demo",
    "bybit": "run-bybit-mainnet",
}
ENTITY_NAMES = tuple(ENTITY_SERVICES)
DEFAULT_ENTITY_NAMES = ("reference", "paper", "demo", "smtp")
SERVICE_NAMES = (*tuple(ENTITY_SERVICES.values()), "smtp-monitor.service")
SECRET_PATTERN = re.compile(
    r"(?im)^([^\n=]*(?:api[_ -]?(?:key|secret)|password|authorization|bearer)[^\n=]*=)[^\n]*$"
)


@dataclass(frozen=True)
class Layout:
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
        return self.monitor_dir or self.project.parent / "smtp_monitor"

    @property
    def monitor_env(self) -> Path:
        return self.monitor_root / "smtp_monitor.env"


Runner = Callable[..., subprocess.CompletedProcess[str]]


def resolve_layout(project: Path | None = None, uv: Path | None = None) -> Layout:
    root = (project or Path(__file__).resolve().parents[1] / "server_bot").resolve()
    uv_value = uv or (Path(found) if (found := shutil.which("uv")) else None)
    if uv_value is None:
        raise RuntimeError("uv is not installed or is not available in PATH")
    return Layout(
        project=root,
        uv=uv_value.resolve(),
        config=root / "paper_bot_config.json",
        paper_env=root / "bybitapi.env",
        demo_env=root / "bybitapidemo.env",
        bybit_env=root / "bybitrealapi.env",
        unit_dir=Path.home() / ".config/systemd/user",
        monitor_dir=root.parent / "smtp_monitor",
    )


def selected_entities(entities: Sequence[str] | None = None) -> tuple[str, ...]:
    selected = tuple(entities or DEFAULT_ENTITY_NAMES)
    unknown = sorted(set(selected) - set(ENTITY_NAMES))
    if unknown:
        raise ValueError("Unknown service entities: " + ", ".join(unknown))
    return tuple(dict.fromkeys(selected))


def service_names(entities: Sequence[str] | None = None) -> tuple[str, ...]:
    return tuple(ENTITY_SERVICES[name] for name in selected_entities(entities))


def unit_names(entities: Sequence[str] | None = None) -> tuple[str, ...]:
    names = list(service_names(entities))
    if "smtp" in selected_entities(entities):
        names.insert(names.index("smtp-monitor.timer"), "smtp-monitor.service")
    return tuple(names)


def validate_layout(layout: Layout, entities: Sequence[str] | None = None) -> None:
    selected = selected_entities(entities)
    required = [layout.project / "pyproject.toml", layout.config]
    credential_paths = []
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
    insecure = [str(path) for path in credential_paths if path.is_file() and path.stat().st_mode & 0o077]
    if insecure:
        raise RuntimeError("Credential file permissions must be 600: " + ", ".join(insecure))


def _quote(value: Path) -> str:
    return '"' + str(value).replace("%", "%%").replace('"', '\\"') + '"'


def unit_contents(layout: Layout) -> dict[str, str]:
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
        "paper-bot-reference.service": (
            "Reference paper bot",
            f"{_quote(layout.uv)} run python -m reference_bot.cli run --resume --config {_quote(layout.config)}",
        ),
        "paper-bot-live.service": (
            "Live paper bot",
            f"{_quote(layout.uv)} run python -m live_paper_bot.cli run --resume --config {_quote(layout.config)}",
        ),
        "bybit-demo.service": (
            "Bybit Demo bot",
            f"{_quote(layout.uv)} run python -m server_bot.cli run-bybit-demo --resume "
            f"--config {_quote(layout.config)} --env {_quote(layout.demo_env)}",
        ),
        "bybit-mainnet.service": (
            "Bybit mainnet bot (REAL FUNDS)",
            f"{_quote(layout.uv)} run python -m server_bot.cli run-bybit-mainnet --confirm-live --resume "
            f"--config {_quote(layout.config)} --env {_quote(layout.bybit_env)}",
        ),
    }
    units = {
        name: f"[Unit]\nDescription={description}\n{common}ExecStart={command}\n{suffix}"
        for name, (description, command) in commands.items()
    }
    monitor_command = (
        f"{_quote(layout.uv)} run --project {_quote(layout.project)} python "
        f"{_quote(layout.monitor_root / 'monitor.py')} --project {_quote(layout.project)} "
        f"--env {_quote(layout.monitor_env)} --state {_quote(layout.monitor_root / 'alert_state.json')}"
    )
    units["smtp-monitor.service"] = (
        "[Unit]\nDescription=SMTP health monitor for reference and Bybit Demo bots\n"
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


def run(command: Sequence[str], *, check: bool = True, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, text=True, capture_output=capture_output)


def install(layout: Layout, runner: Runner = run, *, start: bool = True, entities: Sequence[str] | None = None) -> None:
    selected = selected_entities(entities)
    names = service_names(selected)
    files = unit_names(selected)
    validate_layout(layout, selected)
    layout.unit_dir.mkdir(parents=True, exist_ok=True)
    units = unit_contents(layout)
    for name in files:
        contents = units[name]
        (layout.unit_dir / name).write_text(contents, encoding="utf-8")
    runner(["systemd-analyze", "--user", "verify", *(str(layout.unit_dir / n) for n in files)])
    runner(["systemctl", "--user", "daemon-reload"])
    runner(["systemctl", "--user", "enable", *names])
    if start:
        runner(["systemctl", "--user", "start", *names])


def services_action(action: str, runner: Runner = run, entities: Sequence[str] | None = None) -> None:
    runner(["systemctl", "--user", action, *service_names(entities)])


def uninstall(layout: Layout, runner: Runner = run, entities: Sequence[str] | None = None) -> None:
    names = service_names(entities)
    runner(["systemctl", "--user", "disable", "--now", *names], check=False)
    for name in unit_names(entities):
        (layout.unit_dir / name).unlink(missing_ok=True)
    runner(["systemctl", "--user", "daemon-reload"])


def check_services(runner: Runner = run, entities: Sequence[str] | None = None) -> bool:
    selected = selected_entities(entities)
    names = service_names(selected)
    good = True
    for state in ("is-enabled", "is-active"):
        result = runner(["systemctl", "--user", state, *names], check=False, capture_output=True)
        if result.returncode:
            good = False
            output = (result.stdout + result.stderr).strip()
            if output:
                print(output)
    processes = runner(
        ["pgrep", "-af", "|".join(ENTITY_MARKERS[name] for name in selected if name in ENTITY_MARKERS)],
        check=False,
        capture_output=True,
    )
    lines = [
        line
        for line in processes.stdout.splitlines()
        if line.strip() and "uv run " not in line
    ]
    for marker in (ENTITY_MARKERS[name] for name in selected if name in ENTITY_MARKERS):
        count = sum(marker in line for line in lines)
        if count != 1:
            print(f"Expected exactly one {marker} process, found {count}.")
            good = False
    return good


def redact(text: str) -> str:
    return SECRET_PATTERN.sub(r"\1<redacted>", text)


def _capture(runner: Runner, command: Sequence[str]) -> str:
    result = runner(command, check=False, capture_output=True)
    return redact(result.stdout + result.stderr)


def collect_logs(layout: Layout, output_dir: Path, runner: Runner = run, *, since: str = "7 days ago", entities: Sequence[str] | None = None) -> Path:
    names = unit_names(entities)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = output_dir / f"bot-diagnostics-{stamp}"
    report.mkdir(parents=True, exist_ok=False)
    for service in names:
        contents = _capture(
            runner,
            ["journalctl", "--user-unit", service, "--since", since, "--no-pager", "-o", "short-iso-precise"],
        )
        (report / f"{service}.log").write_text(contents, encoding="utf-8")
    commands = {
        "services-status.txt": ["systemctl", "--user", "status", *names, "--no-pager"],
        "bot-status.txt": [str(layout.uv), "run", "--project", str(layout.project), "python", "-m",
                           "server_bot.cli", "status", "--config", str(layout.config)],
        "bot-stats.txt": [str(layout.uv), "run", "--project", str(layout.project), "python", "-m",
                          "server_bot.cli", "stats", "--config", str(layout.config)],
    }
    for filename, command in commands.items():
        (report / filename).write_text(_capture(runner, command), encoding="utf-8")
    archive = output_dir / f"{report.name}.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(report, arcname=report.name)
    return archive


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, help="server_bot project directory")
    parser.add_argument("--uv", type=Path, help="absolute path to uv")
    commands = parser.add_subparsers(dest="command", required=True)
    install_parser = commands.add_parser("install")
    install_parser.add_argument("--no-start", action="store_true")
    install_parser.add_argument("--entity", action="append", choices=ENTITY_NAMES, dest="entities")
    for command in ("start", "stop", "restart", "status", "check", "uninstall"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--entity", action="append", choices=ENTITY_NAMES, dest="entities")
    logs = commands.add_parser("collect-logs")
    logs.add_argument("--since", default="7 days ago")
    logs.add_argument("--output-dir", type=Path, default=Path.cwd())
    logs.add_argument("--entity", action="append", choices=ENTITY_NAMES, dest="entities")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        layout = resolve_layout(args.project, args.uv)
        if args.command == "install":
            install(layout, start=not args.no_start, entities=args.entities)
        elif args.command in {"start", "stop", "restart", "status"}:
            services_action(args.command, entities=args.entities)
        elif args.command == "check":
            return 0 if check_services(entities=args.entities) else 1
        elif args.command == "uninstall":
            uninstall(layout, entities=args.entities)
        else:
            print(collect_logs(layout, args.output_dir.resolve(), since=args.since, entities=args.entities))
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
