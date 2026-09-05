#!/usr/bin/env python3
"""Read-only health monitor for the reference and Bybit Demo bots."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import smtplib
import ssl
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SERVICES = ("paper-bot-reference.service", "bybit-demo.service")
INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
ERROR_MARKERS = ("error", "failed", "trading halted", "too many visits", "retrying after")


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    username: str
    password: str
    sender: str
    recipients: tuple[str, ...]
    security: str = "ssl"
    alarm_after_seconds: int = 120
    reminder_seconds: int = 3600
    stale_candles: int = 3
    divergence_candles: int = 2
    error_window_minutes: int = 10
    repeated_errors: int = 2


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"Cannot read monitor environment file {path}: {exc}") from exc
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ValueError(f"Invalid monitor environment entry on line {number}")
        name, value = stripped.split("=", 1)
        values[name.strip()] = value.strip().strip("\"'")
    return values


def _required(values: dict[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing required setting {name}")
    return value


def settings_from(values: dict[str, str]) -> Settings:
    security = values.get("SMTP_SECURITY", "ssl").lower()
    if security not in {"ssl", "starttls"}:
        raise ValueError("SMTP_SECURITY must be ssl or starttls")
    recipients = tuple(item.strip() for item in _required(values, "SMTP_TO").split(",") if item.strip())
    try:
        result = Settings(
            host=_required(values, "SMTP_HOST"),
            port=int(values.get("SMTP_PORT", "465" if security == "ssl" else "587")),
            username=_required(values, "SMTP_USERNAME"),
            password=_required(values, "SMTP_PASSWORD"),
            sender=_required(values, "SMTP_FROM"),
            recipients=recipients,
            security=security,
            alarm_after_seconds=int(values.get("ALARM_AFTER_SECONDS", "120")),
            reminder_seconds=int(values.get("REMINDER_SECONDS", "3600")),
            stale_candles=int(values.get("STALE_CANDLES", "3")),
            divergence_candles=int(values.get("DIVERGENCE_CANDLES", "2")),
            error_window_minutes=int(values.get("ERROR_WINDOW_MINUTES", "10")),
            repeated_errors=int(values.get("REPEATED_ERRORS", "2")),
        )
    except ValueError as exc:
        raise ValueError(f"Invalid numeric monitor setting: {exc}") from exc
    if not recipients or min(result.port, result.alarm_after_seconds, result.reminder_seconds,
                             result.stale_candles, result.divergence_candles,
                             result.error_window_minutes, result.repeated_errors) < 1:
        raise ValueError("Monitor thresholds, SMTP port, and recipients must be positive/non-empty")
    return result


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Invalid object in {path.name}")
    return value


def _timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def service_active(name: str, run: Callable = subprocess.run) -> bool:
    result = run(["systemctl", "--user", "is-active", "--quiet", name], check=False)
    return result.returncode == 0


def reference_side(project: Path, symbol: str = "XRPUSDT") -> str | None:
    account = project / "reference_account.json"
    if not account.exists():
        return None
    position = _read_json(account).get("positions", {}).get(symbol)
    return position.get("side") if isinstance(position, dict) and position.get("side") in {"Long", "Short"} else None


def demo_side(project: Path, symbol: str = "XRPUSDT", opener: Callable = urlopen) -> str | None:
    values = load_env(project / "bybitapidemo.env")
    key, secret = _required(values, "BYBIT_API_KEY"), _required(values, "BYBIT_API_SECRET")
    params = {"category": "linear", "symbol": symbol}
    query, timestamp, window = urlencode(sorted(params.items())), str(int(time.time() * 1000)), "10000"
    signature = hmac.new(secret.encode(), f"{timestamp}{key}{window}{query}".encode(), hashlib.sha256).hexdigest()
    request = Request(f"https://api-demo.bybit.com/v5/position/list?{query}", headers={
        "X-BAPI-API-KEY": key, "X-BAPI-TIMESTAMP": timestamp, "X-BAPI-RECV-WINDOW": window,
        "X-BAPI-SIGN": signature, "User-Agent": "server-bot-smtp-monitor/1.0",
    })
    try:
        with opener(request, timeout=20) as response:
            payload = json.loads(response.read().decode())
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Bybit Demo position check failed: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("retCode") != 0:
        message = payload.get("retMsg", "unexpected response") if isinstance(payload, dict) else "unexpected response"
        raise RuntimeError(f"Bybit Demo position check failed: {message}")
    rows = payload.get("result", {}).get("list", [])
    position = next((row for row in rows if float(row.get("size", 0)) > 0), None)
    return str(position["side"]) if position else None


def recent_error_count(minutes: int, run: Callable = subprocess.run) -> int:
    count = 0
    for service in SERVICES:
        result = run(
            ["journalctl", "--user-unit", service, "--since", f"{minutes} minutes ago",
             "--no-pager", "-o", "cat"], check=False, capture_output=True, text=True,
        )
        count += sum(any(marker in line.lower() for marker in ERROR_MARKERS) for line in result.stdout.splitlines())
    return count


def collect_issues(project: Path, settings: Settings, now: datetime, run: Callable = subprocess.run,
                   opener: Callable = urlopen) -> dict[str, str]:
    issues: dict[str, str] = {}
    for service in SERVICES:
        if not service_active(service, run):
            issues[f"service:{service}"] = f"Service is not active: {service}"
    try:
        reference = _read_json(project / "reference_state.json")
        demo = _read_json(project / "bybit_demo_state.json")
        config = _read_json(project / "paper_bot_config.json")
        interval = INTERVAL_SECONDS[str(config["timeframe"])]
        cursors = {
            "reference": _timestamp(reference.get("last_processed_candle")),
            "demo": _timestamp(demo.get("last_processed_candle")),
        }
        for name, cursor in cursors.items():
            age = float("inf") if cursor is None else (now - cursor).total_seconds()
            if age > settings.stale_candles * interval:
                issues[f"stale:{name}"] = f"{name} cursor is stale: {cursor or 'missing'}"
        if all(cursors.values()):
            gap = abs((cursors["reference"] - cursors["demo"]).total_seconds()) / interval
            if gap > settings.divergence_candles:
                issues["cursor-divergence"] = f"Reference/demo cursor difference is {gap:.1f} candles"
        if demo.get("halted_reason"):
            issues["demo-halted"] = f"Bybit Demo trading halted: {demo['halted_reason']}"
        if demo.get("pending_protection_side"):
            issues["demo-protection"] = f"Bybit Demo protection remains pending: {demo['pending_protection_side']}"
    except (KeyError, RuntimeError, ValueError) as exc:
        issues["state"] = str(exc)
    try:
        config = _read_json(project / "paper_bot_config.json")
        symbol = str(config["ticker"]).replace("_", "")
        expected, actual = reference_side(project, symbol), demo_side(project, symbol, opener)
        mapped = {"Long": "Buy", "Short": "Sell", None: None}[expected]
        if mapped != actual:
            issues["position-divergence"] = f"Position mismatch: reference={expected or 'FLAT'}, demo={actual or 'FLAT'}"
    except (KeyError, RuntimeError, ValueError) as exc:
        issues["position-check"] = str(exc)
    errors = recent_error_count(settings.error_window_minutes, run)
    if errors >= settings.repeated_errors:
        issues["repeated-errors"] = f"{errors} bot errors in the last {settings.error_window_minutes} minutes"
    return issues


def send_mail(settings: Settings, subject: str, body: str, smtp_ssl: Callable = smtplib.SMTP_SSL,
              smtp: Callable = smtplib.SMTP) -> None:
    message = EmailMessage()
    message["Subject"], message["From"], message["To"] = subject, settings.sender, ", ".join(settings.recipients)
    message.set_content(body)
    context = ssl.create_default_context()
    client_factory = smtp_ssl if settings.security == "ssl" else smtp
    with client_factory(settings.host, settings.port, timeout=20) as client:
        if settings.security == "starttls":
            client.starttls(context=context)
        client.login(settings.username, settings.password)
        client.send_message(message)


def update_alerts(issues: dict[str, str], previous: dict, now: datetime, settings: Settings) -> tuple[dict, list[str], list[str]]:
    timestamp = now.timestamp()
    old = previous.get("issues", {}) if isinstance(previous, dict) else {}
    current, alarms, recoveries = {}, [], []
    for code, message in issues.items():
        prior = old.get(code, {}) if isinstance(old.get(code), dict) else {}
        first = float(prior.get("first_seen", timestamp))
        last_sent = prior.get("last_sent")
        due = timestamp - first >= settings.alarm_after_seconds
        reminder = last_sent is None or timestamp - float(last_sent) >= settings.reminder_seconds
        if due and reminder:
            alarms.append(message)
            last_sent = timestamp
        current[code] = {"message": message, "first_seen": first, "last_sent": last_sent}
    for code, prior in old.items():
        if code not in issues and isinstance(prior, dict) and prior.get("last_sent") is not None:
            recoveries.append(str(prior.get("message", code)))
    return {"issues": current}, alarms, recoveries


def save_state(path: Path, state: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def run_monitor(project: Path, env_path: Path, state_path: Path, now: datetime | None = None) -> int:
    settings = settings_from(load_env(env_path))
    checked_at = now or datetime.now(timezone.utc)
    try:
        previous = _read_json(state_path) if state_path.exists() else {}
    except RuntimeError:
        previous = {}
    issues = collect_issues(project, settings, checked_at)
    state, alarms, recoveries = update_alerts(issues, previous, checked_at, settings)
    hostname = os.uname().nodename
    if alarms:
        send_mail(settings, f"ALARM: server bots on {hostname}", "\n".join(f"- {item}" for item in alarms))
    if recoveries:
        send_mail(settings, f"RECOVERY: server bots on {hostname}", "\n".join(f"- Resolved: {item}" for item in recoveries))
    save_state(state_path, state)
    print(f"monitor: {len(issues)} issue(s), {len(alarms)} alarm(s), {len(recoveries)} recovery message(s)")
    # Health findings are a successful monitor run. Only configuration,
    # transport, or SMTP failures make the one-shot systemd unit fail.
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run_monitor(args.project.resolve(), args.env.resolve(), args.state.resolve())
    except (OSError, RuntimeError, ValueError, smtplib.SMTPException) as exc:
        print(f"SMTP monitor error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
