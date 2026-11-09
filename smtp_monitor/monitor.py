#!/usr/bin/env python3
# Copyright 2026 smtp-monitor contributors
"""Read-only health monitor for the reference and Bybit Demo bots."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import smtplib
import ssl
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from contextlib import AbstractContextManager

SERVICES = ("paper-bot-reference.service", "bybit-demo.service")
INTERVAL_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}
ERROR_MARKERS = (
    "error",
    "failed",
    "trading halted",
    "too many visits",
    "retrying after",
)


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
        msg = f"Cannot read monitor environment file {path}: {exc}"
        raise RuntimeError(msg) from exc
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            msg = f"Invalid monitor environment entry on line {number}"
            raise ValueError(msg)
        name, value = stripped.split("=", 1)
        values[name.strip()] = value.strip().strip("\"'")
    return values


def _required(values: dict[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        msg = f"Missing required setting {name}"
        raise ValueError(msg)
    return value


def settings_from(values: dict[str, str]) -> Settings:
    security = values.get("SMTP_SECURITY", "ssl").lower()
    if security not in {"ssl", "starttls"}:
        msg = "SMTP_SECURITY must be ssl or starttls"
        raise ValueError(msg)
    recipients = tuple(
        item.strip()
        for item in _required(values, "SMTP_TO").split(",")
        if item.strip()
    )
    try:
        result = Settings(
            host=_required(values, "SMTP_HOST"),
            port=int(
                values.get("SMTP_PORT", "465" if security == "ssl" else "587")
            ),
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
        msg = f"Invalid numeric monitor setting: {exc}"
        raise ValueError(msg) from exc
    if (
        not recipients
        or min(
            result.port,
            result.alarm_after_seconds,
            result.reminder_seconds,
            result.stale_candles,
            result.divergence_candles,
            result.error_window_minutes,
            result.repeated_errors,
        )
        < 1
    ):
        msg = (
            "Monitor thresholds, SMTP port, and recipients must be"
            " positive/non-empty"
        )
        raise ValueError(msg)
    return result


def _object(value: object) -> dict[str, object]:
    """Require a JSON object with string keys.

    Returns:
        The validated object entries.

    Raises:
        TypeError: If the value is not a JSON object.
        ValueError: If any object key is not a string.

    """
    if not isinstance(value, dict):
        msg = "Expected a JSON object"
        raise TypeError(msg)
    # Container membership is established above; validate keys individually.
    entries = cast("Mapping[object, object]", value)
    if any(not isinstance(key, str) for key in entries):
        msg = "Expected string JSON object keys"
        raise ValueError(msg)
    return {str(key): item for key, item in entries.items()}


def _read_json(path: Path) -> dict[str, object]:
    try:
        # JSON decoding is an untyped boundary; schema checks follow below.
        value = cast("object", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"Cannot read {path.name}: {exc}"
        raise RuntimeError(msg) from exc
    try:
        return _object(value)
    except (TypeError, ValueError) as exc:
        msg = f"Invalid object in {path.name}"
        raise RuntimeError(msg) from exc


class CommandRunner(Protocol):
    """Execute health-check commands with optional text capture."""

    def __call__(
        self,
        args: Sequence[str],
        *,
        check: bool = False,
        capture_output: bool = False,
        text: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a command and return its status and text output."""
        ...


class ReadableResponse(Protocol):
    """Read bytes from a downloaded exchange response."""

    def read(self) -> bytes:
        """Return the response body."""
        ...


class ResponseOpener(Protocol):
    """Open an exchange request with a bounded timeout."""

    def __call__(
        self, url: Request, /, *, timeout: float
    ) -> AbstractContextManager[ReadableResponse]:
        """Return a response context manager."""
        ...


class MailConnection(Protocol):
    """SMTP operations needed to deliver one health notification."""

    def starttls(self, *, context: ssl.SSLContext) -> object:
        """Upgrade the connection to TLS."""
        ...

    def login(self, user: str, password: str, /) -> object:
        """Authenticate with the configured SMTP credentials."""
        ...

    def send_message(self, msg: EmailMessage, /) -> object:
        """Deliver the composed notification."""
        ...


class SmtpFactory(Protocol):
    """Construct an SMTP connection with a bounded timeout."""

    def __call__(
        self, host: str, port: int, /, *, timeout: float
    ) -> AbstractContextManager[MailConnection]:
        """Return an SMTP connection context manager."""
        ...


def parse_timestamp(value: object) -> datetime | None:
    """Parse a timezone-aware cursor, including ISO UTC on Python 3.10.

    Returns:
        An aware timestamp, or None for a missing or malformed cursor.

    """
    if not isinstance(value, str):
        return None
    # Python 3.10 does not accept the ISO UTC suffix directly.
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def service_active(name: str, run: CommandRunner = subprocess.run) -> bool:
    result = run(
        ["systemctl", "--user", "is-active", "--quiet", name], check=False
    )
    return result.returncode == 0


def reference_side(project: Path, symbol: str = "XRPUSDT") -> str | None:
    account = project / "reference_account.json"
    if not account.exists():
        return None
    positions = _object(_read_json(account).get("positions", {}))
    position = positions.get(symbol)
    if position is None:
        return None
    side = _object(position).get("side")
    return side if isinstance(side, str) and side in {"Long", "Short"} else None


def demo_side(
    project: Path, symbol: str = "XRPUSDT", opener: ResponseOpener = urlopen
) -> str | None:
    values = load_env(project / "bybitapidemo.env")
    key, secret = _required(values, "BYBIT_API_KEY"), _required(
        values, "BYBIT_API_SECRET"
    )
    params = {"category": "linear", "symbol": symbol}
    query, timestamp, window = (
        urlencode(sorted(params.items())),
        str(int(time.time() * 1000)),
        "10000",
    )
    signature = hmac.new(
        secret.encode(),
        f"{timestamp}{key}{window}{query}".encode(),
        hashlib.sha256,
    ).hexdigest()
    request = Request(
        f"https://api-demo.bybit.com/v5/position/list?{query}",
        headers={
            "X-BAPI-API-KEY": key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": window,
            "X-BAPI-SIGN": signature,
            "User-Agent": "server-bot-smtp-monitor/1.0",
        },
    )
    try:
        with opener(request, timeout=20) as response:
            payload = _object(
                cast("object", json.loads(response.read().decode()))
            )
    except (HTTPError, URLError, TimeoutError, TypeError, ValueError) as exc:
        msg = f"Bybit Demo position check failed: {exc}"
        raise RuntimeError(msg) from exc
    if payload.get("retCode") != 0:
        message = payload.get("retMsg", "unexpected response")
        msg = f"Bybit Demo position check failed: {message}"
        raise RuntimeError(msg)
    try:
        rows = _object(payload.get("result", {})).get("list", [])
        return _active_demo_side(rows)
    except (TypeError, ValueError) as exc:
        msg = f"Bybit Demo position check failed: {exc}"
        raise RuntimeError(msg) from exc


def _active_demo_side(rows: object) -> Literal["Buy", "Sell"] | None:
    """Validate exchange rows and select the first nonzero position.

    Returns:
        The exchange side, or None when every position is flat.

    Raises:
        TypeError: If rows are not a list or position sizes have invalid types.
        ValueError: If position sizes or active sides have invalid values.

    """
    if not isinstance(rows, list):
        msg = "Expected a position list"
        raise TypeError(msg)
    for value in cast("list[object]", rows):
        row = _object(value)
        size = row.get("size", 0)
        if isinstance(size, bool) or not isinstance(size, (str, int, float)):
            msg = "Invalid position size"
            raise TypeError(msg)
        quantity = float(size)
        if not math.isfinite(quantity) or quantity < 0:
            msg = "Invalid position size"
            raise ValueError(msg)
        if quantity == 0:
            continue
        side = row.get("side")
        if side == "Buy":
            return "Buy"
        if side == "Sell":
            return "Sell"
        msg = "Invalid active position side"
        raise ValueError(msg)
    return None


def recent_error_count(
    minutes: int, run: CommandRunner = subprocess.run
) -> int:
    count = 0
    for service in SERVICES:
        result = run(
            [
                "journalctl",
                "--user-unit",
                service,
                "--since",
                f"{minutes} minutes ago",
                "--no-pager",
                "-o",
                "cat",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        count += sum(
            any(marker in line.lower() for marker in ERROR_MARKERS)
            for line in result.stdout.splitlines()
        )
    return count


def collect_issues(
    project: Path,
    settings: Settings,
    now: datetime,
    run: CommandRunner = subprocess.run,
    opener: ResponseOpener = urlopen,
) -> dict[str, str]:
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
            "reference": parse_timestamp(
                reference.get("last_processed_candle")
            ),
            "demo": parse_timestamp(demo.get("last_processed_candle")),
        }
        for name, cursor in cursors.items():
            age = (
                float("inf")
                if cursor is None
                else (now - cursor).total_seconds()
            )
            if age > settings.stale_candles * interval:
                issues[f"stale:{name}"] = (
                    f"{name} cursor is stale: {cursor or 'missing'}"
                )
        reference_cursor, demo_cursor = cursors["reference"], cursors["demo"]
        if reference_cursor is not None and demo_cursor is not None:
            gap = (
                abs((reference_cursor - demo_cursor).total_seconds()) / interval
            )
            if gap > settings.divergence_candles:
                issues["cursor-divergence"] = (
                    f"Reference/demo cursor difference is {gap:.1f} candles"
                )
        if demo.get("halted_reason"):
            issues["demo-halted"] = (
                f"Bybit Demo trading halted: {demo['halted_reason']}"
            )
        if demo.get("pending_protection_side"):
            issues["demo-protection"] = (
                "Bybit Demo protection remains pending:"
                f" {demo['pending_protection_side']}"
            )
    except (KeyError, RuntimeError, TypeError, ValueError) as exc:
        issues["state"] = str(exc)
    try:
        config = _read_json(project / "paper_bot_config.json")
        symbol = str(config["ticker"]).replace("_", "")
        expected, actual = reference_side(project, symbol), demo_side(
            project, symbol, opener
        )
        mapped = {"Long": "Buy", "Short": "Sell", None: None}[expected]
        if mapped != actual:
            issues["position-divergence"] = (
                f"Position mismatch: reference={expected or 'FLAT'},"
                f" demo={actual or 'FLAT'}"
            )
    except (KeyError, RuntimeError, TypeError, ValueError) as exc:
        issues["position-check"] = str(exc)
    errors = recent_error_count(settings.error_window_minutes, run)
    if errors >= settings.repeated_errors:
        issues["repeated-errors"] = (
            f"{errors} bot errors in the last {settings.error_window_minutes}"
            " minutes"
        )
    return issues


def send_mail(
    settings: Settings,
    subject: str,
    body: str,
    smtp_ssl: SmtpFactory = smtplib.SMTP_SSL,
    smtp: SmtpFactory = smtplib.SMTP,
) -> None:
    message = EmailMessage()
    message["Subject"], message["From"], message["To"] = (
        subject,
        settings.sender,
        ", ".join(settings.recipients),
    )
    message.set_content(body)
    context = ssl.create_default_context()
    client_factory = smtp_ssl if settings.security == "ssl" else smtp
    with client_factory(settings.host, settings.port, timeout=20) as client:
        if settings.security == "starttls":
            _ = client.starttls(context=context)
        _ = client.login(settings.username, settings.password)
        _ = client.send_message(message)


class AlertRecord(TypedDict):
    """Validated timestamps and message for one persistent health finding."""

    message: str
    first_seen: float
    last_sent: float | None


class AlertState(TypedDict):
    """Persisted monitor findings, indexed by stable issue code."""

    issues: dict[str, AlertRecord]


def _alert_timestamp(value: object) -> float:
    """Validate a persisted alert timestamp.

    Returns:
        A finite Unix timestamp.

    Raises:
        TypeError: If the timestamp has an unsupported type.
        ValueError: If the timestamp is malformed or nonfinite.

    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        msg = "Invalid alert timestamp"
        raise TypeError(msg)
    timestamp = float(value)
    if not math.isfinite(timestamp):
        msg = "Invalid alert timestamp"
        raise ValueError(msg)
    return timestamp


def update_alerts(
    issues: dict[str, str], previous: object, now: datetime, settings: Settings
) -> tuple[AlertState, list[str], list[str]]:
    timestamp = now.timestamp()
    document = (
        _object(cast("object", previous)) if isinstance(previous, dict) else {}
    )
    old = _object(document.get("issues", {}))
    current: dict[str, AlertRecord] = {}
    alarms: list[str] = []
    recoveries: list[str] = []
    for code, message in issues.items():
        saved = old.get(code)
        prior = (
            _object(cast("object", saved)) if isinstance(saved, dict) else {}
        )
        first = _alert_timestamp(prior.get("first_seen", timestamp))
        sent = prior.get("last_sent")
        last_sent = None if sent is None else _alert_timestamp(sent)
        due = timestamp - first >= settings.alarm_after_seconds
        reminder = (
            last_sent is None
            or timestamp - last_sent >= settings.reminder_seconds
        )
        if due and reminder:
            alarms.append(message)
            last_sent = timestamp
        current[code] = {
            "message": message,
            "first_seen": first,
            "last_sent": last_sent,
        }
    for code, saved in old.items():
        if code not in issues and isinstance(saved, dict):
            prior = _object(cast("object", saved))
            if prior.get("last_sent") is not None:
                recoveries.append(str(prior.get("message", code)))
    return {"issues": current}, alarms, recoveries


def save_state(path: Path, state: Mapping[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    _ = temporary.write_text(
        json.dumps(state, indent=2) + "\n", encoding="utf-8"
    )
    _ = Path(temporary).replace(path)


def run_monitor(
    project: Path, env_path: Path, state_path: Path, now: datetime | None = None
) -> int:
    settings = settings_from(load_env(env_path))
    checked_at = now or datetime.now(UTC)
    try:
        previous = _read_json(state_path) if state_path.exists() else {}
    except RuntimeError:
        previous = {}
    issues = collect_issues(project, settings, checked_at)
    state, alarms, recoveries = update_alerts(
        issues, previous, checked_at, settings
    )
    hostname = os.uname().nodename
    if alarms:
        send_mail(
            settings,
            f"ALARM: server bots on {hostname}",
            "\n".join(f"- {item}" for item in alarms),
        )
    if recoveries:
        send_mail(
            settings,
            f"RECOVERY: server bots on {hostname}",
            "\n".join(f"- Resolved: {item}" for item in recoveries),
        )
    save_state(state_path, state)
    print(
        f"monitor: {len(issues)} issue(s), {len(alarms)} alarm(s),"
        f" {len(recoveries)} recovery message(s)"
    )
    # Health findings are a successful monitor run. Only configuration,
    # transport, or SMTP failures make the one-shot systemd unit fail.
    return 0


class Arguments(argparse.Namespace):
    """Required monitor paths populated by argparse."""

    project: Path = Path()
    env: Path = Path()
    state: Path = Path()


def parse_args(argv: Sequence[str] | None = None) -> Arguments:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--project", type=Path, required=True)
    _ = parser.add_argument("--env", type=Path, required=True)
    _ = parser.add_argument("--state", type=Path, required=True)
    return parser.parse_args(argv, namespace=Arguments())


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run_monitor(
            args.project.resolve(), args.env.resolve(), args.state.resolve()
        )
    except (
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        smtplib.SMTPException,
    ) as exc:
        print(f"SMTP monitor error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
