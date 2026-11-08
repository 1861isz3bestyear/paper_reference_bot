from pathlib import Path
from subprocess import CompletedProcess

import pytest

import bot_services as services


class FakeRunner:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        if self.responses:
            return self.responses.pop(0)
        return CompletedProcess(command, 0, "", "")


@pytest.fixture
def layout(tmp_path):
    project = tmp_path / "server_bot"
    project.mkdir()
    for name in ("pyproject.toml", "paper_bot_config.json", "bybitapi.env", "bybitapidemo.env", "bybitrealapi.env"):
        (project / name).write_text("x", encoding="utf-8")
    (project / "bybitapi.env").chmod(0o600)
    (project / "bybitapidemo.env").chmod(0o600)
    (project / "bybitrealapi.env").chmod(0o600)
    monitor = tmp_path / "smtp_monitor"
    monitor.mkdir()
    (monitor / "monitor.py").write_text("pass\n", encoding="utf-8")
    (monitor / "smtp_monitor.env").write_text("SMTP_PASSWORD=x\n", encoding="utf-8")
    (monitor / "smtp_monitor.env").chmod(0o600)
    uv = tmp_path / "uv"
    uv.touch()
    return services.Layout(project, uv, project / "paper_bot_config.json", project / "bybitapi.env",
                           project / "bybitapidemo.env", project / "bybitrealapi.env", tmp_path / "units", monitor)


def test_resolve_layout_and_missing_uv(monkeypatch, tmp_path):
    uv = tmp_path / "uv"
    uv.touch()
    monkeypatch.setattr(services.shutil, "which", lambda _: str(uv))
    monkeypatch.setattr(services.Path, "home", lambda: tmp_path)
    result = services.resolve_layout(tmp_path / "project")
    assert result.uv == uv.resolve()
    assert result.unit_dir == tmp_path / ".config/systemd/user"
    monkeypatch.setattr(services.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="uv is not installed"):
        services.resolve_layout(tmp_path)


def test_validate_layout_reports_missing(layout):
    layout.config.unlink()
    layout.demo_env.unlink()
    with pytest.raises(RuntimeError) as error:
        services.validate_layout(layout)
    assert "paper_bot_config.json" in str(error.value)
    assert "bybitapidemo.env" in str(error.value)


def test_validate_layout_rejects_insecure_credentials(layout):
    layout.paper_env.chmod(0o644)
    with pytest.raises(RuntimeError, match="permissions must be 600"):
        services.validate_layout(layout)


def test_units_have_safe_expected_commands(layout):
    units = services.unit_contents(layout)
    assert set(units) == set(services.SERVICE_NAMES)
    assert "reference_bot.cli run --resume" in units["paper-bot-reference.service"]
    assert "live_paper_bot.cli run --resume" in units["paper-bot-live.service"]
    assert "run-bybit-demo --resume" in units["bybit-demo.service"]
    assert "run-bybit-mainnet --confirm-live --resume" in units["bybit-mainnet.service"]
    for name in services.ENTITY_SERVICES.values():
        if name.endswith(".timer"):
            continue
        text = units[name]
        if name == "smtp-monitor.service":
            assert "Type=oneshot" in text and "monitor.py" in text
            continue
        assert "Restart=on-failure" in text and "KillSignal=SIGINT" in text
        assert f"WorkingDirectory={layout.project}" in text
        assert f'WorkingDirectory="{layout.project}"' not in text
        assert " reset" not in text
    assert "OnBootSec=15sec" in units["smtp-monitor.timer"]
    assert "OnUnitActiveSec=1min" in units["smtp-monitor.timer"]


def test_install_writes_verifies_enables_and_optionally_starts(layout):
    runner = FakeRunner()
    services.install(layout, runner)
    assert all((layout.unit_dir / name).is_file() for name in services.service_names())
    assert not (layout.unit_dir / "bybit-mainnet.service").exists()
    assert (layout.unit_dir / "smtp-monitor.service").exists()
    assert (layout.unit_dir / "smtp-monitor.timer").exists()
    assert runner.calls[0][0][:3] == ["systemd-analyze", "--user", "verify"]
    assert runner.calls[-2][0][2] == "enable"
    assert runner.calls[-1][0][2] == "start"
    runner = FakeRunner()
    services.install(layout, runner, start=False)
    assert not any(call[0][2:3] == ["start"] for call in runner.calls)


def test_install_selected_entities_validates_only_their_credentials(layout):
    layout.demo_env.unlink()
    layout.bybit_env.unlink()
    runner = FakeRunner()
    services.install(layout, runner, entities=("reference", "paper"))
    assert (layout.unit_dir / "paper-bot-reference.service").is_file()
    assert (layout.unit_dir / "paper-bot-live.service").is_file()
    assert not (layout.unit_dir / "bybit-demo.service").exists()
    assert runner.calls[-1][0][-2:] == ["paper-bot-reference.service", "paper-bot-live.service"]


def test_entity_selection_helpers_reject_unknown_and_deduplicate():
    assert services.selected_entities(("demo", "demo")) == ("demo",)
    assert services.service_names(("bybit",)) == ("bybit-mainnet.service",)
    assert services.unit_names(("smtp",)) == ("smtp-monitor.service", "smtp-monitor.timer")
    with pytest.raises(ValueError, match="Unknown"):
        services.selected_entities(("wrong",))


def test_service_action_and_uninstall(layout):
    runner = FakeRunner()
    services.services_action("restart", runner)
    assert runner.calls[0][0] == ["systemctl", "--user", "restart", *services.service_names()]
    layout.unit_dir.mkdir(exist_ok=True)
    for name in services.service_names():
        (layout.unit_dir / name).touch()
    services.uninstall(layout, runner)
    assert not any(layout.unit_dir.iterdir())
    assert runner.calls[1][1]["check"] is False


def test_check_services_success_and_failures(capsys):
    processes = (
        "1 uv run python -m reference_bot.cli\n"
        "2 reference_bot.cli\n"
        "3 uv run python -m live_paper_bot.cli\n"
        "4 live_paper_bot.cli\n"
        "5 uv run python -m server_bot.cli run-bybit-demo\n"
        "6 run-bybit-demo\n"
        "7 uv run python -m server_bot.cli run-bybit-mainnet\n"
        "8 run-bybit-mainnet\n"
    )
    okay = FakeRunner([CompletedProcess([], 0, "", ""), CompletedProcess([], 0, "", ""),
                       CompletedProcess([], 0, processes, "")])
    assert services.check_services(okay)
    bad = FakeRunner([CompletedProcess([], 1, "disabled\n", "problem\n"), CompletedProcess([], 0, "", ""),
                      CompletedProcess([], 0, processes + "4 run-bybit-demo\n", "")])
    assert not services.check_services(bad)
    output = capsys.readouterr().out
    assert "disabled" in output and "found 2" in output


def test_redact_and_collect_logs(layout, tmp_path, monkeypatch):
    text = "BYBIT_API_KEY=abc\napi-secret = xyz\nSMTP_PASSWORD=hidden\nnormal=value\n"
    redacted = services.redact(text)
    assert "abc" not in redacted and "xyz" not in redacted and "hidden" not in redacted and "normal=value" in redacted
    monkeypatch.setattr(services, "datetime", type("Clock", (), {
        "now": staticmethod(lambda _: type("Now", (), {"strftime": lambda self, _: "STAMP"})())
    }))
    runner = FakeRunner([CompletedProcess([], 0, "BYBIT_API_KEY=secret\n", "")] * 9)
    archive = services.collect_logs(layout, tmp_path, runner, since="1 hour ago")
    assert archive.name == "bot-diagnostics-STAMP.tar.gz" and archive.is_file()
    report = tmp_path / "bot-diagnostics-STAMP"
    assert len(list(report.iterdir())) == 8
    assert "secret" not in (report / "bybit-demo.service.log").read_text()
    assert runner.calls[0][0][3:5] == ["--since", "1 hour ago"]
    assert runner.calls[0][0][1:3] == ["--user-unit", "paper-bot-reference.service"]
    assert runner.calls[6][0][1:4] == ["run", "--project", str(layout.project)]


@pytest.mark.parametrize("command", ["start", "stop", "restart", "status"])
def test_main_service_actions(monkeypatch, command):
    seen = []
    monkeypatch.setattr(services, "resolve_layout", lambda *_: object())
    monkeypatch.setattr(services, "services_action", lambda action, entities: seen.append((action, entities)))
    assert services.main([command]) == 0 and seen == [(command, None)]


def test_main_other_branches_and_error(monkeypatch, tmp_path, capsys):
    layout = object()
    monkeypatch.setattr(services, "resolve_layout", lambda *_: layout)
    installed = []
    monkeypatch.setattr(services, "install", lambda value, start, entities: installed.append((value, start, entities)))
    assert services.main(["install", "--no-start", "--entity", "bybit"]) == 0 and installed == [(layout, False, ["bybit"])]
    monkeypatch.setattr(services, "check_services", lambda entities: False)
    assert services.main(["check"]) == 1
    removed = []
    monkeypatch.setattr(services, "uninstall", lambda value, entities: removed.append((value, entities)))
    assert services.main(["uninstall"]) == 0 and removed == [(layout, None)]
    archive = tmp_path / "report.tar.gz"
    monkeypatch.setattr(services, "collect_logs", lambda *args, **kwargs: archive)
    assert services.main(["collect-logs", "--output-dir", str(tmp_path)]) == 0
    assert str(archive) in capsys.readouterr().out
    monkeypatch.setattr(services, "resolve_layout", lambda *_: (_ for _ in ()).throw(RuntimeError("bad")))
    assert services.main(["status"]) == 1
    assert "Error: bad" in capsys.readouterr().err
