import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest

import monitor


NOW = datetime(2026, 9, 5, 6, 0, tzinfo=timezone.utc)


def values(**changes):
    result = {
        "SMTP_HOST": "smtp.test", "SMTP_PORT": "465", "SMTP_USERNAME": "user",
        "SMTP_PASSWORD": "password", "SMTP_FROM": "from@test", "SMTP_TO": "a@test,b@test",
    }
    result.update(changes)
    return result


def write_project(root: Path, *, reference_cursor=None, demo_cursor=None, reference_side=None,
                  halted=None, pending=None):
    root.mkdir()
    (root / "paper_bot_config.json").write_text('{"timeframe":"1m","ticker":"XRP_USDT"}')
    (root / "reference_state.json").write_text(json.dumps({"last_processed_candle": reference_cursor}))
    (root / "bybit_demo_state.json").write_text(json.dumps({
        "last_processed_candle": demo_cursor, "halted_reason": halted,
        "pending_protection_side": pending,
    }))
    (root / "bybitapidemo.env").write_text("BYBIT_API_KEY=k\nBYBIT_API_SECRET=s\n")
    if reference_side:
        (root / "reference_account.json").write_text(json.dumps({
            "positions": {"XRPUSDT": {"side": reference_side}}
        }))


class Response:
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return None
    def read(self):
        return json.dumps(self.payload).encode()


def position_response(side=None):
    rows = [] if side is None else [{"side": side, "size": "2"}]
    return Response({"retCode": 0, "result": {"list": rows}})


def test_load_and_validate_settings(tmp_path):
    env = tmp_path / "env"
    env.write_text("# comment\nSMTP_HOST='smtp.test'\nSMTP_USERNAME=u\nSMTP_PASSWORD=p\nSMTP_FROM=f@x\nSMTP_TO=a@x, b@x\nSMTP_SECURITY=starttls\n")
    loaded = monitor.settings_from(monitor.load_env(env))
    assert loaded.host == "smtp.test" and loaded.port == 587 and loaded.recipients == ("a@x", "b@x")
    with pytest.raises(RuntimeError, match="Cannot read"):
        monitor.load_env(tmp_path / "missing")
    env.write_text("broken")
    with pytest.raises(ValueError, match="line 1"):
        monitor.load_env(env)
    for change, message in [({"SMTP_HOST": ""}, "SMTP_HOST"), ({"SMTP_SECURITY": "plain"}, "ssl or starttls"),
                            ({"SMTP_PORT": "bad"}, "numeric"), ({"STALE_CANDLES": "0"}, "positive")]:
        with pytest.raises(ValueError, match=message):
            monitor.settings_from(values(**change))


def test_json_timestamp_and_reference_side(tmp_path):
    assert monitor._timestamp("2026-01-01T00:00:00Z") is not None
    assert monitor._timestamp("bad") is None
    assert monitor.reference_side(tmp_path) is None
    (tmp_path / "reference_account.json").write_text('{"positions":{"XRPUSDT":{"side":"Long"}}}')
    assert monitor.reference_side(tmp_path) == "Long"
    (tmp_path / "reference_account.json").write_text("[]")
    with pytest.raises(RuntimeError, match="Invalid object"):
        monitor.reference_side(tmp_path)
    (tmp_path / "reference_account.json").write_text("bad")
    with pytest.raises(RuntimeError, match="Cannot read"):
        monitor.reference_side(tmp_path)


def test_demo_side_signs_request_and_handles_errors(tmp_path):
    (tmp_path / "bybitapidemo.env").write_text("BYBIT_API_KEY=k\nBYBIT_API_SECRET=s\n")
    seen = []
    assert monitor.demo_side(tmp_path, opener=lambda request, timeout: seen.append(request) or position_response("Buy")) == "Buy"
    assert "X-bapi-sign" in seen[0].headers and "api-demo.bybit.com" in seen[0].full_url
    assert monitor.demo_side(tmp_path, opener=lambda *_a, **_k: position_response()) is None
    with pytest.raises(RuntimeError, match="bad key"):
        monitor.demo_side(tmp_path, opener=lambda *_a, **_k: Response({"retCode": 1, "retMsg": "bad key"}))
    with pytest.raises(RuntimeError, match="position check failed"):
        monitor.demo_side(tmp_path, opener=Mock(side_effect=TimeoutError("slow")))


def test_service_and_journal_checks():
    run = Mock(side_effect=[CompletedProcess([], 0), CompletedProcess([], 1)])
    assert monitor.service_active("one", run)
    assert not monitor.service_active("two", run)
    run = Mock(side_effect=[
        CompletedProcess([], 0, "normal\nretrying after error: bad\n", ""),
        CompletedProcess([], 0, "failed once\n", ""),
    ])
    assert monitor.recent_error_count(10, run) == 2
    assert "10 minutes ago" in run.call_args_list[0].args[0]


def test_collect_issues_healthy_and_unhealthy(tmp_path):
    project = tmp_path / "project"
    cursor = (NOW - timedelta(minutes=1)).isoformat()
    write_project(project, reference_cursor=cursor, demo_cursor=cursor, reference_side="Long")
    okay_run = Mock(return_value=CompletedProcess([], 0, "", ""))
    settings = monitor.settings_from(values())
    assert monitor.collect_issues(project, settings, NOW, okay_run,
                                  lambda *_a, **_k: position_response("Buy")) == {}

    (project / "reference_state.json").write_text(json.dumps({
        "last_processed_candle": (NOW - timedelta(minutes=10)).isoformat()
    }))
    (project / "bybit_demo_state.json").write_text(json.dumps({
        "last_processed_candle": cursor, "halted_reason": "unsafe", "pending_protection_side": "Buy"
    }))
    bad_run = Mock(side_effect=[CompletedProcess([], 1), CompletedProcess([], 0),
                                CompletedProcess([], 0, "error x\nerror y\n", ""), CompletedProcess([], 0, "", "")])
    issues = monitor.collect_issues(project, settings, NOW, bad_run,
                                    lambda *_a, **_k: position_response("Sell"))
    assert {"service:paper-bot-reference.service", "stale:reference", "cursor-divergence",
            "demo-halted", "demo-protection", "position-divergence", "repeated-errors"} <= issues.keys()


def test_collect_issues_reports_bad_state_and_position_api(tmp_path):
    project = tmp_path / "project"
    write_project(project)
    (project / "reference_state.json").write_text("bad")
    run = Mock(return_value=CompletedProcess([], 0, "", ""))
    issues = monitor.collect_issues(project, monitor.settings_from(values()), NOW, run,
                                    Mock(side_effect=TimeoutError()))
    assert "state" in issues and "position-check" in issues


def test_alert_delay_reminders_and_recovery():
    settings = monitor.settings_from(values())
    state, alarms, recovery = monitor.update_alerts({"x": "broken"}, {}, NOW, settings)
    assert alarms == recovery == []
    later = NOW + timedelta(seconds=120)
    state, alarms, recovery = monitor.update_alerts({"x": "broken"}, state, later, settings)
    assert alarms == ["broken"] and not recovery
    state, alarms, _ = monitor.update_alerts({"x": "still broken"}, state, later + timedelta(minutes=1), settings)
    assert not alarms
    state, alarms, _ = monitor.update_alerts({"x": "still broken"}, state, later + timedelta(hours=1), settings)
    assert alarms == ["still broken"]
    _, _, recovery = monitor.update_alerts({}, state, later + timedelta(hours=1, minutes=1), settings)
    assert recovery == ["still broken"]


class SMTP:
    def __init__(self, *_a, **_k):
        self.calls = []
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return None
    def starttls(self, **kwargs):
        self.calls.append(("tls", kwargs))
    def login(self, *args):
        self.calls.append(("login", args))
    def send_message(self, message):
        self.calls.append(("send", message))


def test_send_mail_ssl_and_starttls():
    created = []
    factory = lambda *_a, **_k: created.append(SMTP()) or created[-1]
    monitor.send_mail(monitor.settings_from(values()), "subject", "body", smtp_ssl=factory)
    assert created[0].calls[-1][1]["Subject"] == "subject"
    monitor.send_mail(monitor.settings_from(values(SMTP_SECURITY="starttls")), "s", "b", smtp=factory)
    assert created[1].calls[0][0] == "tls"


def test_save_run_and_main(monkeypatch, tmp_path, capsys):
    env, state, project = tmp_path / "env", tmp_path / "state.json", tmp_path / "project"
    env.write_text("\n".join(f"{k}={v}" for k, v in values(ALARM_AFTER_SECONDS="1").items()))
    project.mkdir()
    monkeypatch.setattr(monitor, "collect_issues", lambda *_a, **_k: {"x": "problem"})
    sent = []
    monkeypatch.setattr(monitor, "send_mail", lambda _s, subject, body: sent.append((subject, body)))
    assert monitor.run_monitor(project, env, state, NOW) == 0
    existing = json.loads(state.read_text())
    existing["issues"]["x"]["first_seen"] = (NOW - timedelta(seconds=2)).timestamp()
    state.write_text(json.dumps(existing))
    assert monitor.run_monitor(project, env, state, NOW) == 0 and "ALARM" in sent[0][0]
    monkeypatch.setattr(monitor, "collect_issues", lambda *_a, **_k: {})
    assert monitor.run_monitor(project, env, state, NOW) == 0 and "RECOVERY" in sent[-1][0]
    assert monitor.main(["--project", str(project), "--env", str(tmp_path / "missing"), "--state", str(state)]) == 2
    assert "SMTP monitor error" in capsys.readouterr().out
