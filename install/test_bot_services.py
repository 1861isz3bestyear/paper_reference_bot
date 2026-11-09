# Copyright 2026 avwap-workspace-tests contributors

import shutil
from collections.abc import Sequence
from datetime import tzinfo
from pathlib import Path
from subprocess import CompletedProcess

import bot_services as services
import pytest


class FakeRunner:
    """Record service commands and return queued process results."""

    calls: list[tuple[list[str], dict[str, bool]]]
    responses: list[CompletedProcess[str]]

    def __init__(
        self, responses: Sequence[CompletedProcess[str]] | None = None
    ) -> None:
        """Start with no recorded calls and optional queued responses."""
        self.calls = []
        self.responses = list(responses or [])

    def __call__(
        self, command: Sequence[str], **kwargs: bool
    ) -> CompletedProcess[str]:
        """Record a command and consume a queued result when available.

        Returns:
            The next queued result or a successful empty process result.

        """
        self.calls.append((list(command), kwargs))
        if self.responses:
            return self.responses.pop(0)
        return CompletedProcess(command, 0, "", "")


@pytest.fixture
def layout(tmp_path: Path) -> services.Layout:
    project = tmp_path / "server_bot"
    project.mkdir()
    for name in (
        "pyproject.toml",
        "paper_bot_config.json",
        "bybitapi.env",
        "bybitapidemo.env",
        "bybitrealapi.env",
    ):
        _ = (project / name).write_text("x", encoding="utf-8")
    (project / "bybitapi.env").chmod(0o600)
    (project / "bybitapidemo.env").chmod(0o600)
    (project / "bybitrealapi.env").chmod(0o600)
    monitor = tmp_path / "smtp_monitor"
    monitor.mkdir()
    _ = (monitor / "monitor.py").write_text("pass\n", encoding="utf-8")
    _ = (monitor / "smtp_monitor.env").write_text(
        "SMTP_PASSWORD=x\n", encoding="utf-8"
    )
    (monitor / "smtp_monitor.env").chmod(0o600)
    uv = tmp_path / "uv"
    uv.touch()
    return services.Layout(
        project,
        uv,
        project / "paper_bot_config.json",
        project / "bybitapi.env",
        project / "bybitapidemo.env",
        project / "bybitrealapi.env",
        tmp_path / "units",
        monitor,
    )


def test_resolve_layout_and_missing_uv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify resolve layout and missing uv."""
    uv = tmp_path / "uv"
    uv.touch()

    def available_uv(_command: str) -> str:
        return str(uv)

    monkeypatch.setattr(shutil, "which", available_uv)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = services.resolve_layout(tmp_path / "project")
    assert result.uv == uv.resolve()
    assert result.unit_dir == tmp_path / ".config/systemd/user"

    def missing_uv(_command: str) -> None:
        return None

    monkeypatch.setattr(shutil, "which", missing_uv)
    with pytest.raises(RuntimeError, match="uv is not installed"):
        _ = services.resolve_layout(tmp_path)


def test_validate_layout_reports_missing(layout: services.Layout) -> None:
    """Verify validate layout reports missing."""
    layout.config.unlink()
    layout.demo_env.unlink()
    with pytest.raises(RuntimeError) as error:
        services.validate_layout(layout)
    assert "paper_bot_config.json" in str(error.value)
    assert "bybitapidemo.env" in str(error.value)


def test_validate_layout_rejects_insecure_credentials(
    layout: services.Layout,
) -> None:
    """Verify validate layout rejects insecure credentials."""
    layout.paper_env.chmod(0o644)
    with pytest.raises(RuntimeError, match="permissions must be 600"):
        services.validate_layout(layout)


def test_units_have_safe_expected_commands(layout: services.Layout) -> None:
    """Verify units have safe expected commands."""
    units = services.unit_contents(layout)
    assert set(units) == set(services.SERVICE_NAMES)
    assert (
        "reference_bot.cli run --resume" in units["paper-bot-reference.service"]
    )
    assert "live_paper_bot.cli run --resume" in units["paper-bot-live.service"]
    assert "run-bybit-demo --resume" in units["bybit-demo.service"]
    assert (
        "run-bybit-mainnet --confirm-live --resume"
        in units["bybit-mainnet.service"]
    )
    for name in services.ENTITY_SERVICES.values():
        if name.endswith(".timer"):
            continue
        text = units[name]
        if name == "smtp-monitor.service":
            assert "Type=oneshot" in text
            assert "monitor.py" in text
            continue
        assert "Restart=on-failure" in text
        assert "KillSignal=SIGINT" in text
        assert f"WorkingDirectory={layout.project}" in text
        assert f'WorkingDirectory="{layout.project}"' not in text
        assert " reset" not in text
    assert "OnBootSec=15sec" in units["smtp-monitor.timer"]
    assert "OnUnitActiveSec=1min" in units["smtp-monitor.timer"]


def test_install_writes_verifies_enables_and_optionally_starts(
    layout: services.Layout,
) -> None:
    """Verify install writes verifies enables and optionally starts."""
    runner = FakeRunner()
    services.install(layout, runner)
    assert all(
        (layout.unit_dir / name).is_file() for name in services.service_names()
    )
    assert not (layout.unit_dir / "bybit-mainnet.service").exists()
    assert (layout.unit_dir / "smtp-monitor.service").exists()
    assert (layout.unit_dir / "smtp-monitor.timer").exists()
    assert runner.calls[0][0][:3] == ["systemd-analyze", "--user", "verify"]
    assert runner.calls[-2][0][2] == "enable"
    assert runner.calls[-1][0][2] == "start"
    runner = FakeRunner()
    services.install(layout, runner, start=False)
    assert not any(call[0][2:3] == ["start"] for call in runner.calls)


def test_install_selected_entities_validates_only_their_credentials(
    layout: services.Layout,
) -> None:
    """Verify install selected entities validates only their credentials."""
    layout.demo_env.unlink()
    layout.bybit_env.unlink()
    runner = FakeRunner()
    services.install(layout, runner, entities=("reference", "paper"))
    assert (layout.unit_dir / "paper-bot-reference.service").is_file()
    assert (layout.unit_dir / "paper-bot-live.service").is_file()
    assert not (layout.unit_dir / "bybit-demo.service").exists()
    assert runner.calls[-1][0][-2:] == [
        "paper-bot-reference.service",
        "paper-bot-live.service",
    ]


def test_entity_selection_helpers_reject_unknown_and_deduplicate() -> None:
    """Verify entity selection helpers reject unknown and deduplicate."""
    assert services.selected_entities(("demo", "demo")) == ("demo",)
    assert services.service_names(("bybit",)) == ("bybit-mainnet.service",)
    assert services.unit_names(("smtp",)) == (
        "smtp-monitor.service",
        "smtp-monitor.timer",
    )
    with pytest.raises(ValueError, match="Unknown"):
        _ = services.selected_entities(("wrong",))


def test_service_action_and_uninstall(layout: services.Layout) -> None:
    """Verify service action and uninstall."""
    runner = FakeRunner()
    services.services_action("restart", runner)
    assert runner.calls[0][0] == [
        "systemctl",
        "--user",
        "restart",
        *services.service_names(),
    ]
    layout.unit_dir.mkdir(exist_ok=True)
    for name in services.service_names():
        (layout.unit_dir / name).touch()
    services.uninstall(layout, runner)
    assert not any(layout.unit_dir.iterdir())
    assert runner.calls[1][1]["check"] is False


def test_check_services_success_and_failures(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify check services success and failures."""
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
    okay = FakeRunner([
        CompletedProcess([], 0, "", ""),
        CompletedProcess([], 0, "", ""),
        CompletedProcess([], 0, processes, ""),
    ])
    assert services.check_services(okay)
    bad = FakeRunner([
        CompletedProcess([], 1, "disabled\n", "problem\n"),
        CompletedProcess([], 0, "", ""),
        CompletedProcess([], 0, processes + "4 run-bybit-demo\n", ""),
    ])
    assert not services.check_services(bad)
    output = capsys.readouterr().out
    assert "disabled" in output
    assert "found 2" in output


def test_redact_and_collect_logs(
    layout: services.Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify redact and collect logs."""
    text = (
        "BYBIT_API_KEY=abc\napi-secret ="
        " xyz\nSMTP_PASSWORD=hidden\nnormal=value\n"
    )
    redacted = services.redact(text)
    assert "abc" not in redacted
    assert "xyz" not in redacted
    assert "hidden" not in redacted
    assert "normal=value" in redacted

    class ArchiveClock:
        """Keep the archive filename deterministic without patching a clock."""

        @staticmethod
        def now(_zone: tzinfo) -> ArchiveClock:
            """Return a timestamp formatter for the archive test.

            Returns:
                A formatter with a deterministic timestamp.

            """
            return ArchiveClock()

        @staticmethod
        def strftime(_format: str) -> str:
            """Return the archive timestamp marker.

            Returns:
                The fixed marker used in the expected archive path.

            """
            return "STAMP"

    monkeypatch.setattr(services, "datetime", ArchiveClock)
    runner = FakeRunner(
        [CompletedProcess([], 0, "BYBIT_API_KEY=secret\n", "")] * 9
    )
    archive = services.collect_logs(
        layout, tmp_path, runner, since="1 hour ago"
    )
    assert archive.name == "bot-diagnostics-STAMP.tar.gz"
    assert archive.is_file()
    report = tmp_path / "bot-diagnostics-STAMP"
    assert len(list(report.iterdir())) == 8
    assert "secret" not in (report / "bybit-demo.service.log").read_text()
    assert runner.calls[0][0][3:5] == ["--since", "1 hour ago"]
    assert runner.calls[0][0][1:3] == [
        "--user-unit",
        "paper-bot-reference.service",
    ]
    assert runner.calls[6][0][1:4] == ["run", "--project", str(layout.project)]


@pytest.fixture
def cli_layout(
    layout: services.Layout, monkeypatch: pytest.MonkeyPatch
) -> services.Layout:
    """Resolve CLI paths to the isolated service deployment.

    Returns:
        The test deployment used by the CLI command.

    """

    def resolve(
        _project: Path | None = None, _uv: Path | None = None
    ) -> services.Layout:
        return layout

    monkeypatch.setattr(services, "resolve_layout", resolve)
    return layout


@pytest.mark.parametrize("command", ["start", "stop", "restart", "status"])
def test_main_service_actions(
    monkeypatch: pytest.MonkeyPatch, command: str, cli_layout: services.Layout
) -> None:
    """Dispatch each management action to the selected services."""
    seen: list[tuple[str, Sequence[str] | None]] = []

    def action(name: str, *, entities: Sequence[str] | None) -> None:
        seen.append((name, entities))

    monkeypatch.setattr(services, "services_action", action)
    assert services.main([command]) == 0
    assert seen == [(command, None)]
    assert cli_layout.project.is_dir()


def test_main_install_preserves_selection_and_no_start(
    monkeypatch: pytest.MonkeyPatch, cli_layout: services.Layout
) -> None:
    """Pass the deployment, explicit selection and no-start flag to install."""
    installed: list[tuple[services.Layout, bool, Sequence[str] | None]] = []

    def install(
        value: services.Layout, *, start: bool, entities: Sequence[str] | None
    ) -> None:
        installed.append((value, start, entities))

    monkeypatch.setattr(services, "install", install)
    assert services.main(["install", "--no-start", "--entity", "bybit"]) == 0
    assert installed == [(cli_layout, False, ["bybit"])]


def test_main_failed_check_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, cli_layout: services.Layout
) -> None:
    """Return a failing CLI exit status when the selected services are unhealthy."""

    def check(*, entities: Sequence[str] | None) -> bool:
        assert entities is None
        return False

    monkeypatch.setattr(services, "check_services", check)
    assert services.main(["check"]) == 1
    assert cli_layout.project.is_dir()


def test_main_uninstall_preserves_deployment(
    monkeypatch: pytest.MonkeyPatch, cli_layout: services.Layout
) -> None:
    """Dispatch uninstallation with the resolved deployment and default set."""
    removed: list[tuple[services.Layout, Sequence[str] | None]] = []

    def uninstall(
        value: services.Layout, *, entities: Sequence[str] | None
    ) -> None:
        removed.append((value, entities))

    monkeypatch.setattr(services, "uninstall", uninstall)
    assert services.main(["uninstall"]) == 0
    assert removed == [(cli_layout, None)]


def test_main_log_collection_reports_archive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    cli_layout: services.Layout,
) -> None:
    """Print the archive path after forwarding log collection options."""
    archive = tmp_path / "report.tar.gz"

    def collect(
        value: services.Layout,
        output_dir: Path,
        *,
        since: str,
        entities: Sequence[str] | None,
    ) -> Path:
        assert value is cli_layout
        assert output_dir == tmp_path
        assert since == "7 days ago"
        assert entities is None
        return archive

    monkeypatch.setattr(services, "collect_logs", collect)
    assert services.main(["collect-logs", "--output-dir", str(tmp_path)]) == 0
    assert str(archive) in capsys.readouterr().out


def test_main_reports_layout_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report deployment errors without starting or changing services."""

    def fail(
        _project: Path | None = None, _uv: Path | None = None
    ) -> services.Layout:
        message = "bad deployment"
        raise RuntimeError(message)

    monkeypatch.setattr(services, "resolve_layout", fail)
    assert services.main(["status"]) == 1
    assert "Error: bad deployment" in capsys.readouterr().err


def test_data_saver_only_needs_no_config_or_credentials(
    layout: services.Layout,
) -> None:
    """Verify data saver only needs no config or credentials."""
    layout.config.unlink()
    layout.paper_env.unlink()
    layout.demo_env.unlink()
    layout.bybit_env.unlink()
    runner = FakeRunner()
    services.install(layout, runner, entities=["data-saver"])
    assert [p.name for p in layout.unit_dir.iterdir()] == [
        "paper-data-saver.service"
    ]
    text = (layout.unit_dir / "paper-data-saver.service").read_text()
    assert "python -m server_bot.data_saver" in text
    assert "--config" not in text
    assert "--env" not in text
    assert runner.calls[-1][0] == [
        "systemctl",
        "--user",
        "start",
        "paper-data-saver.service",
    ]


def test_data_saver_logs_do_not_require_trading_state(
    layout: services.Layout, tmp_path: Path
) -> None:
    """Verify data saver logs do not require trading state."""
    runner = FakeRunner()
    archive = services.collect_logs(
        layout, tmp_path, runner, entities=["data-saver"]
    )
    assert archive.is_file()
    assert len(runner.calls) == 2
    assert all("server_bot.cli" not in call[0] for call in runner.calls)
