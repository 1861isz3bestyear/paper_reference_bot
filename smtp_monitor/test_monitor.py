# Copyright 2026 smtp-monitor contributors
from __future__ import annotations

import json
import ssl
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.request import Request
from types import TracebackType
from subprocess import CompletedProcess
from unittest.mock import Mock

import monitor
import pytest

NOW = datetime(2026, 9, 5, 6, 0, tzinfo=UTC)


def values(**changes: str) -> dict[str, str]:
    result = {
        "SMTP_HOST": "smtp.test",
        "SMTP_PORT": "465",
        "SMTP_USERNAME": "user",
        "SMTP_PASSWORD": "password",
        "SMTP_FROM": "from@test",
        "SMTP_TO": "a@test,b@test",
    }
    result.update(changes)
    return result


def write_project(
    root: Path,
    *,
    reference_cursor: str | None = None,
    demo_cursor: str | None = None,
    reference_side: str | None = None,
    halted: str | None = None,
    pending: str | None = None,
) -> None:
    root.mkdir()
    _ = (root / "paper_bot_config.json").write_text(
        '{"timeframe":"1m","ticker":"XRP_USDT"}'
    )
    _ = (root / "reference_state.json").write_text(
        json.dumps({"last_processed_candle": reference_cursor})
    )
    _ = (root / "bybit_demo_state.json").write_text(
        json.dumps({
            "last_processed_candle": demo_cursor,
            "halted_reason": halted,
            "pending_protection_side": pending,
        })
    )
    _ = (root / "bybitapidemo.env").write_text(
        "BYBIT_API_KEY=k\nBYBIT_API_SECRET=s\n"
    )
    if reference_side:
        _ = (root / "reference_account.json").write_text(
            json.dumps({"positions": {"XRPUSDT": {"side": reference_side}}})
        )


class Response:
    """Context-managed exchange payload for offline HTTP checks."""

    payload: object

    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __enter__(self) -> "Response":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def position_response(side: str | None = None) -> Response:
    rows = [] if side is None else [{"side": side, "size": "2"}]
    return Response({"retCode": 0, "result": {"list": rows}})


def test_load_and_validate_settings(tmp_path: Path) -> None:
    """Verify load and validate settings."""
    env = tmp_path / "env"
    _ = env.write_text(
        "# comment\nSMTP_HOST='smtp.test'\nSMTP_USERNAME=u\nSMTP_PASSWORD=p\nSMTP_FROM=f@x\nSMTP_TO=a@x,"
        " b@x\nSMTP_SECURITY=starttls\n"
    )
    loaded = monitor.settings_from(monitor.load_env(env))
    assert loaded.host == "smtp.test"
    assert loaded.port == 587
    assert loaded.recipients == ("a@x", "b@x")
    with pytest.raises(RuntimeError, match="Cannot read"):
        _ = monitor.load_env(tmp_path / "missing")
    _ = env.write_text("broken")
    with pytest.raises(ValueError, match="line 1"):
        _ = monitor.load_env(env)
    for change, message in [
        ({"SMTP_HOST": ""}, "SMTP_HOST"),
        ({"SMTP_SECURITY": "plain"}, "ssl or starttls"),
        ({"SMTP_PORT": "bad"}, "numeric"),
        ({"STALE_CANDLES": "0"}, "positive"),
    ]:
        with pytest.raises(ValueError, match=message):
            _ = monitor.settings_from(values(**change))


def test_json_timestamp_and_reference_side(tmp_path: Path) -> None:
    """Verify json timestamp and reference side."""
    assert monitor.parse_timestamp("2026-01-01T00:00:00Z") is not None
    assert monitor.parse_timestamp("bad") is None
    assert monitor.reference_side(tmp_path) is None
    _ = (tmp_path / "reference_account.json").write_text(
        '{"positions":{"XRPUSDT":{"side":"Long"}}}'
    )
    assert monitor.reference_side(tmp_path) == "Long"
    _ = (tmp_path / "reference_account.json").write_text("[]")
    with pytest.raises(RuntimeError, match="Invalid object"):
        _ = monitor.reference_side(tmp_path)
    _ = (tmp_path / "reference_account.json").write_text("bad")
    with pytest.raises(RuntimeError, match="Cannot read"):
        _ = monitor.reference_side(tmp_path)


def test_demo_side_signs_request_and_handles_errors(tmp_path: Path) -> None:
    """Verify demo side signs request and handles errors."""
    _ = (tmp_path / "bybitapidemo.env").write_text(
        "BYBIT_API_KEY=k\nBYBIT_API_SECRET=s\n"
    )
    seen: list[Request] = []

    def open_position(url: Request, *, timeout: float) -> Response:
        """Record the signed request and return a long position.

        Returns:
            A deterministic exchange response.

        """
        assert timeout > 0
        seen.append(url)
        return position_response("Buy")

    assert monitor.demo_side(tmp_path, opener=open_position) == "Buy"
    assert "X-bapi-sign" in seen[0].headers
    assert "api-demo.bybit.com" in seen[0].full_url
    assert (
        monitor.demo_side(
            tmp_path, opener=Mock(return_value=position_response())
        )
        is None
    )
    with pytest.raises(RuntimeError, match="bad key"):
        _ = monitor.demo_side(
            tmp_path,
            opener=Mock(
                return_value=Response({"retCode": 1, "retMsg": "bad key"})
            ),
        )
    with pytest.raises(RuntimeError, match="position check failed"):
        _ = monitor.demo_side(
            tmp_path, opener=Mock(side_effect=TimeoutError("slow"))
        )


def test_service_and_journal_checks() -> None:
    """Verify service and journal checks."""
    run = Mock(side_effect=[CompletedProcess([], 0), CompletedProcess([], 1)])
    assert monitor.service_active("one", run)
    assert not monitor.service_active("two", run)
    run = Mock(
        side_effect=[
            CompletedProcess([], 0, "normal\nretrying after error: bad\n", ""),
            CompletedProcess([], 0, "failed once\n", ""),
        ]
    )
    assert monitor.recent_error_count(10, run) == 2
    assert "10 minutes ago" in run.call_args_list[0].args[0]


def test_collect_issues_healthy_and_unhealthy(tmp_path: Path) -> None:
    """Verify collect issues healthy and unhealthy."""
    project = tmp_path / "project"
    cursor = (NOW - timedelta(minutes=1)).isoformat()
    write_project(
        project,
        reference_cursor=cursor,
        demo_cursor=cursor,
        reference_side="Long",
    )
    okay_run = Mock(return_value=CompletedProcess([], 0, "", ""))
    settings = monitor.settings_from(values())
    assert (
        monitor.collect_issues(
            project,
            settings,
            NOW,
            okay_run,
            Mock(return_value=position_response("Buy")),
        )
        == {}
    )
    _ = (project / "reference_state.json").write_text(
        json.dumps(
            {"last_processed_candle": (NOW - timedelta(minutes=10)).isoformat()}
        )
    )
    _ = (project / "bybit_demo_state.json").write_text(
        json.dumps({
            "last_processed_candle": cursor,
            "halted_reason": "unsafe",
            "pending_protection_side": "Buy",
        })
    )
    bad_run = Mock(
        side_effect=[
            CompletedProcess([], 1),
            CompletedProcess([], 0),
            CompletedProcess([], 0, "error x\nerror y\n", ""),
            CompletedProcess([], 0, "", ""),
        ]
    )
    issues = monitor.collect_issues(
        project,
        settings,
        NOW,
        bad_run,
        Mock(return_value=position_response("Sell")),
    )
    assert {
        "service:paper-bot-reference.service",
        "stale:reference",
        "cursor-divergence",
        "demo-halted",
        "demo-protection",
        "position-divergence",
        "repeated-errors",
    } <= issues.keys()


def test_collect_issues_reports_bad_state_and_position_api(
    tmp_path: Path,
) -> None:
    """Verify collect issues reports bad state and position api."""
    project = tmp_path / "project"
    write_project(project)
    _ = (project / "reference_state.json").write_text("bad")
    run = Mock(return_value=CompletedProcess([], 0, "", ""))
    issues = monitor.collect_issues(
        project,
        monitor.settings_from(values()),
        NOW,
        run,
        Mock(side_effect=TimeoutError()),
    )
    assert "state" in issues
    assert "position-check" in issues


def test_alert_delay_reminders_and_recovery() -> None:
    """Verify alert delay reminders and recovery."""
    settings = monitor.settings_from(values())
    state, alarms, recovery = monitor.update_alerts(
        {"x": "broken"}, {}, NOW, settings
    )
    assert alarms == recovery == []
    later = NOW + timedelta(seconds=120)
    state, alarms, recovery = monitor.update_alerts(
        {"x": "broken"}, state, later, settings
    )
    assert alarms == ["broken"]
    assert not recovery
    state, alarms, _ = monitor.update_alerts(
        {"x": "still broken"}, state, later + timedelta(minutes=1), settings
    )
    assert not alarms
    state, alarms, _ = monitor.update_alerts(
        {"x": "still broken"}, state, later + timedelta(hours=1), settings
    )
    assert alarms == ["still broken"]
    _, _, recovery = monitor.update_alerts(
        {}, state, later + timedelta(hours=1, minutes=1), settings
    )
    assert recovery == ["still broken"]


class SMTP:
    """Record authentication, TLS, and delivery without contacting a server."""

    calls: list[str]
    messages: list[EmailMessage]

    def __init__(self) -> None:
        """Start with no connection operations or delivered messages."""
        self.calls = []
        self.messages = []

    def __enter__(self) -> SMTP:
        """Enter the test connection context."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the test connection without swallowing exceptions."""

    def starttls(self, *, context: ssl.SSLContext) -> None:
        """Record the TLS upgrade using the supplied verification context."""
        assert context.verify_mode == ssl.CERT_REQUIRED
        self.calls.append("tls")

    def login(self, user: str, password: str) -> None:
        """Require the configured test credentials before authentication."""
        assert (user, password) == ("user", "password")
        self.calls.append("login")

    def send_message(self, msg: EmailMessage) -> None:
        """Record the complete notification for content assertions."""
        self.messages.append(msg)
        self.calls.append("send")


def test_send_mail_ssl_and_starttls() -> None:
    """Verify send mail ssl and starttls."""
    created: list[SMTP] = []

    def factory(host: str, port: int, *, timeout: float) -> SMTP:
        """Record the SMTP destination and create an offline connection.

        Returns:
            A connection that records delivered notifications.

        """
        assert host == "smtp.test"
        assert port > 0
        assert timeout > 0
        client = SMTP()
        created.append(client)
        return client

    monitor.send_mail(
        monitor.settings_from(values()), "subject", "body", smtp_ssl=factory
    )
    assert created[0].messages[-1]["Subject"] == "subject"
    monitor.send_mail(
        monitor.settings_from(values(SMTP_SECURITY="starttls")),
        "s",
        "b",
        smtp=factory,
    )
    assert created[1].calls[0] == "tls"


def test_save_run_and_main(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify save run and main."""
    env, state, project = (
        tmp_path / "env",
        tmp_path / "state.json",
        tmp_path / "project",
    )
    _ = env.write_text(
        "\n".join(
            f"{k}={v}" for k, v in values(ALARM_AFTER_SECONDS="1").items()
        )
    )
    project.mkdir()
    monkeypatch.setattr(
        monitor, "collect_issues", Mock(return_value={"x": "problem"})
    )
    sent: list[tuple[str, str]] = []

    def send_notification(
        settings: monitor.Settings, subject: str, body: str
    ) -> None:
        """Capture delivery and require the configured test destination."""
        assert settings.host == "smtp.test"
        sent.append((subject, body))

    monkeypatch.setattr(monitor, "send_mail", send_notification)
    assert monitor.run_monitor(project, env, state, NOW) == 0
    monitor.save_state(
        state,
        {
            "issues": {
                "x": {
                    "message": "problem",
                    "first_seen": (NOW - timedelta(seconds=2)).timestamp(),
                    "last_sent": None,
                }
            }
        },
    )
    assert monitor.run_monitor(project, env, state, NOW) == 0
    assert "ALARM" in sent[0][0]
    monkeypatch.setattr(monitor, "collect_issues", Mock(return_value={}))
    assert monitor.run_monitor(project, env, state, NOW) == 0
    assert "RECOVERY" in sent[-1][0]
    assert (
        monitor.main([
            "--project",
            str(project),
            "--env",
            str(tmp_path / "missing"),
            "--state",
            str(state),
        ])
        == 2
    )
    assert "SMTP monitor error" in capsys.readouterr().out


@pytest.mark.parametrize(
    "rows",
    [
        None,
        {},
        [None],
        [{"size": True}],
        [{"size": "nan"}],
        [{"size": "-1"}],
        [{"size": "2", "side": "Long"}],
    ],
)
def test_demo_side_rejects_malformed_positions(
    tmp_path: Path, rows: object
) -> None:
    """Reject malformed exchange positions instead of reporting a valid side."""
    _ = (tmp_path / "bybitapidemo.env").write_text(
        "BYBIT_API_KEY=k\nBYBIT_API_SECRET=s\n"
    )

    def open_response(url: Request, *, timeout: float) -> Response:
        """Return the invalid exchange payload without sending a request.

        Returns:
            The malformed position rows in an otherwise valid response.

        """
        assert url.full_url.startswith("https://api-demo.bybit.com/")
        assert timeout > 0
        return Response({"retCode": 0, "result": {"list": rows}})

    with pytest.raises(RuntimeError, match="position check failed"):
        _ = monitor.demo_side(tmp_path, opener=open_response)


@pytest.mark.parametrize("timestamp", [True, None, [], "nan", "inf"])
def test_alerts_reject_malformed_timestamps(timestamp: object) -> None:
    """Reject invalid persistent timestamps before computing alert delays."""
    previous = {"issues": {"stale": {"first_seen": timestamp}}}
    with pytest.raises(
        (TypeError, ValueError), match="Invalid alert timestamp"
    ):
        _ = monitor.update_alerts(
            {"stale": "Stale cursor"},
            previous,
            NOW,
            monitor.settings_from(values()),
        )
