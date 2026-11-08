# Copyright 2026 anchored-vwam-backtest contributors
"""Exercise deterministic exchange error and command boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

import pytest

from live_paper_bot import market as live_module
from reference_bot import market as reference_module
from server_bot import cli, tunnel_archive

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(params=[False, True])
def live(request: pytest.FixtureRequest) -> bool:
    """Select a deployed bot and isolate its runtime artifacts.

    Returns:
        Whether the live-paper implementation is selected.

    Raises:
        TypeError: If a fixture parameter is not a boolean.

    """
    value = cast("object", request.param)
    if not isinstance(value, bool):
        message = "Expected a boolean implementation selector."
        raise TypeError(message)
    return value


@pytest.mark.parametrize(
    "payload",
    [
        {"retCode": 1},
        {"retCode": 0},
        {"retCode": 0, "result": {"list": [{"lastPrice": "0"}]}},
    ],
)
def test_bad_bybit_price(
    *, live: bool, payload: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject failed, missing and nonpositive Bybit quotes."""
    market = live_module if live else reference_module
    response = Mock(json=Mock(return_value=payload))
    monkeypatch.setattr("requests.get", Mock(return_value=response))
    with pytest.raises(ValueError, match=r"Bybit"):
        _ = market.fetch_live_price("BTCUSDT")


@pytest.mark.parametrize(
    "payload", [dict[str, object](), {"price": "0"}, {"price": "-1"}]
)
def test_bad_mexc_price(
    *, live: bool, payload: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject missing and nonpositive spot quotes."""
    market = live_module if live else reference_module
    response = Mock(json=Mock(return_value=payload))
    monkeypatch.setattr("requests.get", Mock(return_value=response))
    with pytest.raises(ValueError, match=r"MEXC"):
        _ = market.fetch_mexc_live_price("BTCUSDT")


@pytest.mark.parametrize(
    "payload",
    [
        dict[str, object](),
        {"symbols": [dict[str, object]()]},
        {"symbols": [{"takerCommission": "-1"}]},
    ],
)
def test_bad_mexc_fee(
    *, live: bool, payload: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject absent and negative spot commissions."""
    market = live_module if live else reference_module
    response = Mock(json=Mock(return_value=payload))
    monkeypatch.setattr("requests.get", Mock(return_value=response))
    with pytest.raises(ValueError, match=r"MEXC"):
        _ = market.fetch_mexc_taker_fee_rate("BTCUSDT")


def test_mexc_kline_error(
    *, live: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Report API errors and unsupported intervals without inventing candles."""
    market = live_module if live else reference_module
    response = Mock(json=Mock(return_value={"msg": "unavailable"}))
    monkeypatch.setattr("requests.get", Mock(return_value=response))
    with pytest.raises(ValueError, match=r"API error"):
        _ = market.fetch_completed_mexc_klines("BTCUSDT", "1m", 0, 60000)
    with pytest.raises(ValueError, match=r"does not support"):
        _ = market.fetch_completed_mexc_klines("BTCUSDT", "2m", 0, 60000)


@pytest.mark.parametrize(
    ("command", "function"),
    [
        ("start", "start_both"),
        ("stop", "stop_both"),
        ("reset", "reset_both"),
        ("status", "status_both"),
        ("stats", "stats_both"),
        ("run-bybit-demo", "run_demo_command"),
        ("run-bybit-mainnet", "run_bybit_command"),
    ],
)
def test_server_dispatch(
    command: str, function: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dispatch combined commands with no external process or order effects."""
    monkeypatch.setattr("sys.argv", ["server", command])
    action = Mock()
    monkeypatch.setattr(cli, function, action)
    cli.main()
    action.assert_called_once()


def test_health_command_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preserve the health result as a process status."""
    monkeypatch.setattr("sys.argv", ["server", "health-check"])
    monkeypatch.setattr(
        cli, "health_check", Mock(return_value=(2, "unavailable"))
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    expected_value = 2
    if error.value.code != expected_value:
        pytest.fail("Expected error.value.code == 2")


def test_archive_cli_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Return a failing exit when a one-pass collection loses a source."""
    monkeypatch.setattr(
        "sys.argv",
        ["archive", "--once", "--output", str(tmp_path / "archive.sqlite")],
    )
    monkeypatch.setattr(tunnel_archive, "collect", Mock(return_value=1))
    with pytest.raises(SystemExit) as error:
        tunnel_archive.main()
    if error.value.code != 1:
        pytest.fail("Expected error.value.code == 1")
    monkeypatch.setattr("sys.argv", ["archive", "--poll-seconds", "0"])
    with pytest.raises(SystemExit) as error:
        tunnel_archive.main()
    expected_value = 2
    if error.value.code != expected_value:
        pytest.fail("Expected error.value.code == 2")


def test_archive_margin_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sign the read-only margin endpoint without persisting credentials."""
    monkeypatch.setenv("BYBIT_API_KEY", "fixture-key")
    monkeypatch.setenv("BYBIT_API_SECRET", "fixture-secret")
    get = Mock(return_value=Mock(text='{"result": {}}'))
    session = Mock(get=get)
    failures = tunnel_archive.collect(tmp_path / "archive.sqlite", session)
    if failures != 0:
        pytest.fail("Expected failures == 0")
    if "interest-rate-history?currency=USDT" not in get.call_args.args[0]:
        pytest.fail(
            "Expected 'interest-rate-history?currency=USDT' in"
            " get.call_args.args[0]"
        )
    if get.call_args.kwargs["headers"]["X-BAPI-API-KEY"] != "fixture-key":
        pytest.fail(
            "Expected get.call_args.kwargs['headers']['X-BAPI-API-KEY'] =="
            " 'fixture-key'"
        )
    with pytest.raises(ValueError, match=r"BTC or ETH"):
        _ = tunnel_archive.sources("DOGE")
