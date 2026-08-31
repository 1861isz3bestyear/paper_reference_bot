#!/usr/bin/env python3
"""Install, manage, verify, and diagnose the three user systemd bot services."""

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

SERVICE_NAMES = ("paper-bot-reference.service", "paper-bot-live.service", "bybit-demo.service")
SECRET_PATTERN = re.compile(
    r"(?im)^([^\n=]*(?:api[_ -]?(?:key|secret)|authorization|bearer)[^\n=]*=)[^\n]*$"
)


@dataclass(frozen=True)
class Layout:
    project: Path
    uv: Path
    config: Path
    paper_env: Path
    demo_env: Path
    unit_dir: Path


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
        unit_dir=Path.home() / ".config/systemd/user",
    )


def validate_layout(layout: Layout) -> None:
    required = (layout.project / "pyproject.toml", layout.config, layout.paper_env, layout.demo_env)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("Missing required file(s): " + ", ".join(missing))
    insecure = [str(path) for path in (layout.paper_env, layout.demo_env) if path.stat().st_mode & 0o077]
    if insecure:
        raise RuntimeError("Credential file permissions must be 600: " + ", ".join(insecure))


def _quote(value: Path) -> str:
    return '"' + str(value).replace("%", "%%").replace('"', '\\"') + '"'


def unit_contents(layout: Layout) -> dict[str, str]:
    common = (
        "After=network-online.target\nWants=network-online.target\n\n"
        "[Service]\nType=simple\n"
        f"WorkingDirectory={_quote(layout.project)}\n"
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
    }
    return {
        name: f"[Unit]\nDescription={description}\n{common}ExecStart={command}\n{suffix}"
        for name, (description, command) in commands.items()
    }


def run(command: Sequence[str], *, check: bool = True, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=check, text=True, capture_output=capture_output)


def install(layout: Layout, runner: Runner = run, *, start: bool = True) -> None:
    validate_layout(layout)
    layout.unit_dir.mkdir(parents=True, exist_ok=True)
    for name, contents in unit_contents(layout).items():
        (layout.unit_dir / name).write_text(contents, encoding="utf-8")
    runner(["systemd-analyze", "--user", "verify", *(str(layout.unit_dir / n) for n in SERVICE_NAMES)])
    runner(["systemctl", "--user", "daemon-reload"])
    runner(["systemctl", "--user", "enable", *SERVICE_NAMES])
    if start:
        runner(["systemctl", "--user", "start", *SERVICE_NAMES])


def services_action(action: str, runner: Runner = run) -> None:
    runner(["systemctl", "--user", action, *SERVICE_NAMES])


def uninstall(layout: Layout, runner: Runner = run) -> None:
    runner(["systemctl", "--user", "disable", "--now", *SERVICE_NAMES], check=False)
    for name in SERVICE_NAMES:
        (layout.unit_dir / name).unlink(missing_ok=True)
    runner(["systemctl", "--user", "daemon-reload"])


def check_services(runner: Runner = run) -> bool:
    good = True
    for state in ("is-enabled", "is-active"):
        result = runner(["systemctl", "--user", state, *SERVICE_NAMES], check=False, capture_output=True)
        if result.returncode:
            good = False
            output = (result.stdout + result.stderr).strip()
            if output:
                print(output)
    processes = runner(
        ["pgrep", "-af", "reference_bot.cli|live_paper_bot.cli|run-bybit-demo"],
        check=False,
        capture_output=True,
    )
    lines = [line for line in processes.stdout.splitlines() if line.strip()]
    for marker in ("reference_bot.cli", "live_paper_bot.cli", "run-bybit-demo"):
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


def collect_logs(layout: Layout, output_dir: Path, runner: Runner = run, *, since: str = "7 days ago") -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = output_dir / f"bot-diagnostics-{stamp}"
    report.mkdir(parents=True, exist_ok=False)
    for service in SERVICE_NAMES:
        contents = _capture(
            runner,
            ["journalctl", "--user", "-u", service, "--since", since, "--no-pager", "-o", "short-iso-precise"],
        )
        (report / f"{service}.log").write_text(contents, encoding="utf-8")
    commands = {
        "services-status.txt": ["systemctl", "--user", "status", *SERVICE_NAMES, "--no-pager"],
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
    for command in ("start", "stop", "restart", "status", "check", "uninstall"):
        commands.add_parser(command)
    logs = commands.add_parser("collect-logs")
    logs.add_argument("--since", default="7 days ago")
    logs.add_argument("--output-dir", type=Path, default=Path.cwd())
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        layout = resolve_layout(args.project, args.uv)
        if args.command == "install":
            install(layout, start=not args.no_start)
        elif args.command in {"start", "stop", "restart", "status"}:
            services_action(args.command)
        elif args.command == "check":
            return 0 if check_services() else 1
        elif args.command == "uninstall":
            uninstall(layout)
        else:
            print(collect_logs(layout, args.output_dir.resolve(), since=args.since))
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
