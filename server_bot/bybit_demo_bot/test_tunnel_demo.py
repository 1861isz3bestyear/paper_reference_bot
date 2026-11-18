# Copyright 2026 avwap-workspace-tests contributors
"""Exercise causal tunnel signals, bounded demo orders and crash recovery."""

from __future__ import annotations

import json
import signal
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from market_storage.archive import Archive
from market_storage.models import Observation, Source

from bybit_demo_bot import cli, tunnel_executor
from bybit_demo_bot.client import BybitDemoClient, BybitDemoError
from bybit_demo_bot.tunnel_executor import TunnelDemoBot
from bybit_demo_bot.tunnel_journal import TunnelJournal, pending_entry
from bybit_demo_bot.tunnel_orders import (
    Protection,
    positive_decimal,
    protection,
    round_protection,
)
from bybit_demo_bot.tunnel_signals import TunnelDecision, archive_decision
from shared.paper_config import PaperBotConfig
from shared.tunnel import TunnelSettings
from shared.tunnel_config import TUNNEL_MODE
from shared.validation import json_object
from tests.mock_support import mock_child

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 11, 18, 12, tzinfo=UTC)


def tunnel_config() -> PaperBotConfig:
    """Create the exact configuration type exported by the calculator.

    Returns:
        A validated negative/positive GEX configuration with two required votes.

    """
    return PaperBotConfig(
        strategy_mode=TUNNEL_MODE,
        timeframe="5m",
        trend=False,
        open_order_vwap_sigma=1,
        close_order_vwap_sigma=0,
        initial_capital=1000,
        stop_loss_pct=1,
        take_profit_pct=2,
        allow_immediate_reentry=True,
        vwap_anchor_reset_weeks=1,
        tunnel=TunnelSettings(
            use_book=True, use_margin=True, use_oi=True, votes=2
        ),
    )


@pytest.fixture
def bot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TunnelDemoBot:
    """Use a temporary journal and deterministic exchange boundaries.

    Returns:
        A demo executor that cannot access an external exchange.

    """
    client = Mock(spec=BybitDemoClient)
    mock_child(client, "position").return_value = None
    mock_child(client, "has_open_orders").return_value = False
    mock_child(client, "last_price").return_value = Decimal(100)
    mock_child(client, "available_usdt").return_value = Decimal(2000)
    mock_child(client, "instruments").return_value = {
        "lotSizeFilter": {
            "qtyStep": "0.01",
            "minOrderQty": "0.01",
            "minNotionalValue": "5",
            "maxMktOrderQty": "100",
        },
        "priceFilter": {"tickSize": "0.01"},
    }
    monkeypatch.setattr(
        tunnel_executor,
        "archive_decision",
        Mock(
            return_value=TunnelDecision(
                direction=1, state=3, votes=2, target_ceiling=float("nan")
            )
        ),
    )
    return TunnelDemoBot(
        config=tunnel_config(),
        client=client,
        journal=TunnelJournal(
            path=tmp_path / "demo.sqlite", fingerprint="test"
        ),
        archive=tmp_path / "archive.sqlite",
    )


def test_entry_intent_precedes_post_and_survives_restart(
    bot: TunnelDemoBot,
) -> None:
    """An ambiguous POST is recovered by order identity, never replayed."""
    client = bot.client
    assert isinstance(client, Mock)

    def lost_acknowledgement(_: object) -> None:
        assert bot.journal.get("pending") is not None
        message = "connection lost"
        raise BybitDemoError(message)

    mock_child(client, "tunnel_market_order").side_effect = lost_acknowledgement
    with pytest.raises(BybitDemoError, match="connection lost"):
        _ = bot.reconcile_once(NOW)
    pending = bot.journal.get("pending")
    assert pending is not None
    intent = pending_entry(pending)
    assert intent.quantity == Decimal(10)
    assert intent.stop == Decimal(99)
    assert intent.target == Decimal(102)
    mock_child(client, "order_by_link").return_value = None
    with pytest.raises(RuntimeError, match="outcome is unknown"):
        _ = bot.reconcile_once(NOW)
    mock_child(client, "tunnel_market_order").assert_called_once()
    mock_child(client, "position").return_value = {
        "side": "Buy",
        "size": "10",
        "avgPrice": "101",
    }
    mock_child(client, "order_by_link").return_value = {"orderStatus": "Filled"}
    resumed = TunnelDemoBot(
        config=bot.config,
        client=client,
        journal=bot.journal,
        archive=bot.archive,
    )
    assert resumed.reconcile_once(NOW) == "tunnel fill protected"
    mock_child(client, "set_protection").assert_called_with(
        "BTCUSDT", Decimal("99.99"), Decimal("103.02")
    )
    assert bot.journal.get("pending") is None
    assert bot.journal.get("owned") is not None
    assert resumed.reconcile_once(NOW) == "tunnel fill protected"
    mock_child(client, "position").return_value = None
    assert resumed.reconcile_once(NOW) == "candle already processed"


@pytest.mark.parametrize("direction", [-1, 0, 1])
def test_direction_and_once_per_candle(
    bot: TunnelDemoBot, monkeypatch: pytest.MonkeyPatch, direction: int
) -> None:
    """Only eligible signed decisions submit one protected market order."""
    monkeypatch.setattr(
        tunnel_executor,
        "archive_decision",
        Mock(
            return_value=TunnelDecision(
                direction=direction, state=4, votes=2, target_ceiling=101
            )
        ),
    )
    _ = bot.reconcile_once(NOW)
    client = bot.client
    assert isinstance(client, Mock)
    if direction:
        pending = bot.journal.get("pending")
        assert pending is not None
        intent = pending_entry(pending)
        assert intent.side == ("Buy" if direction == 1 else "Sell")
        assert intent.target == (
            Decimal(101) if direction == 1 else Decimal(98)
        )
        mock_child(client, "order_by_link").return_value = {
            "orderStatus": "Cancelled"
        }
        assert "terminal" in bot.reconcile_once(NOW)
    else:
        mock_child(client, "tunnel_market_order").assert_not_called()
    assert bot.reconcile_once(NOW) == "candle already processed"


@pytest.mark.parametrize(
    "status",
    [
        "New",
        "Filled",
        "Cancelled",
        "Rejected",
        "PartiallyFilledCanceled",
        "Deactivated",
    ],
)
def test_terminal_order_reconciliation(bot: TunnelDemoBot, status: str) -> None:
    """Terminal orders release pending state; live orders remain pending."""
    _ = bot.reconcile_once(NOW)
    client = bot.client
    assert isinstance(client, Mock)
    mock_child(client, "order_by_link").return_value = {"orderStatus": status}
    _ = bot.reconcile_once(NOW)
    assert (bot.journal.get("pending") is not None) is (
        status in {"New", "Filled"}
    )


def test_foreign_positions_orders_and_config_are_rejected(
    bot: TunnelDemoBot,
) -> None:
    """The executor never adopts unknown exposure or a different strategy."""
    client = bot.client
    assert isinstance(client, Mock)
    mock_child(client, "position").return_value = {"side": "Buy", "size": "1"}
    with pytest.raises(RuntimeError, match="ownership"):
        _ = bot.reconcile_once(NOW)
    mock_child(client, "position").return_value = None
    mock_child(client, "has_open_orders").return_value = True
    with pytest.raises(RuntimeError, match="Existing BTC orders"):
        _ = bot.reconcile_once(NOW)
    with pytest.raises(ValueError, match="only Snapshot tunnel"):
        _ = TunnelDemoBot(
            config=replace(
                bot.config,
                strategy_mode="AVWAP crossover",
                close_order_vwap_sigma=1,
            ),
            client=client,
            journal=bot.journal,
            archive=bot.archive,
        )
    with pytest.raises(ValueError, match="configuration differs"):
        TunnelJournal(path=bot.journal.path, fingerprint="other").prepare()


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "bad", None])
def test_invalid_exchange_quantities_fail_closed(value: object) -> None:
    """Reject nonpositive, nonfinite and malformed external numbers."""
    with pytest.raises((ValueError, TypeError)):
        _ = positive_decimal(value)


def test_pending_validation_and_grid_protection(bot: TunnelDemoBot) -> None:
    """Validate durable intent and enforce the frozen cap after fills."""
    _ = bot.reconcile_once(NOW)
    content = bot.journal.get("pending")
    assert content is not None
    intent = pending_entry(content)
    capped = replace(intent, ceiling=Decimal(101))
    with pytest.raises(ValueError, match="not profitable"):
        _ = protection(Decimal(102), capped, config=bot.config)
    assert round_protection(
        Protection(stop=Decimal("99.001"), target=Decimal("101.999")),
        Decimal("0.01"),
        side="Buy",
    ) == (Decimal("99.01"), Decimal("101.99"))
    assert round_protection(
        Protection(stop=Decimal("101.999"), target=Decimal("99.001")),
        Decimal("0.01"),
        side="Sell",
    ) == (Decimal("101.99"), Decimal("99.01"))
    values = json_object(content)
    values["side"] = "invalid"
    with pytest.raises(ValueError, match="side"):
        _ = pending_entry(json.dumps(values))
    values["side"], values["ceiling"] = "Buy", "NaN"
    with pytest.raises(ValueError, match="target ceiling"):
        _ = pending_entry(json.dumps(values))
    values["ceiling"] = None
    values["side"], values["quantity"] = "Buy", "-1"
    with pytest.raises(ValueError, match="quantities"):
        _ = pending_entry(json.dumps(values))


def test_real_archive_decision_uses_recorded_negative_gex_and_receipts(
    tmp_path: Path,
) -> None:
    """Run the real archive adapter and shared strategy without GUI imports."""
    archive = Archive(path=tmp_path / "inputs.sqlite")
    now = datetime.now(UTC)
    instrument = "BTC-TEST-P"
    payloads: dict[Source, object] = {
        Source.INSTRUMENTS: {
            "result": [{
                "instrument_name": instrument,
                "kind": "option",
                "is_active": True,
                "base_currency": "BTC",
                "option_type": "put",
                "contract_size": 1,
                "expiration_timestamp": int(
                    (now + timedelta(days=10)).timestamp() * 1000
                ),
                "strike": 100000,
            }]
        },
        Source.OPTIONS: {
            "result": [{
                "instrument_name": instrument,
                "open_interest": 20,
                "mark_iv": 50,
            }]
        },
        Source.SPOT: {"result": {"index_price": 100000}},
        Source.DEPTH: {
            "retCode": 0,
            "result": {
                "s": "BTCUSDT",
                "ts": int(now.timestamp() * 1000),
                "b": [["99999", "5"]],
                "a": [["100001", "1"]],
            },
        },
    }
    for source, payload in payloads.items():
        archive.store(
            Observation(
                asset="BTC",
                source=source,
                requested=now,
                available=now + timedelta(seconds=1),
                payload=json.dumps(payload),
                collection="test",
            )
        )
    config = replace(tunnel_config(), tunnel=TunnelSettings(use_book=True))
    before = archive_decision(archive.path, now, price=100000, config=config)
    after = archive_decision(
        archive.path, now + timedelta(seconds=2), price=100000, config=config
    )
    assert before.direction == 0
    assert after.direction == 1
    assert after.state == -1
    assert after.votes == 1
    stale = archive_decision(
        archive.path, now + timedelta(hours=1), price=100000, config=config
    )
    assert stale.direction == 0


@pytest.mark.parametrize("limit", ["capital", "maximum", "grid"])
def test_entry_limits_fail_before_order_submission(
    bot: TunnelDemoBot, limit: str
) -> None:
    """Reject unfunded, oversized and unprotectable entries before any POST."""
    client = bot.client
    assert isinstance(client, Mock)
    if limit == "capital":
        bot.config = replace(bot.config, initial_capital=0.1)
    elif limit == "maximum":
        mock_child(client, "instruments").return_value = {
            "lotSizeFilter": {
                "qtyStep": "0.01",
                "minOrderQty": "0.01",
                "maxMktOrderQty": "1",
            },
            "priceFilter": {"tickSize": "0.01"},
        }
    else:
        mock_child(client, "instruments").return_value = {
            "lotSizeFilter": {"qtyStep": "0.01", "minOrderQty": "0.01"},
            "priceFilter": {"tickSize": "1000"},
        }
    with pytest.raises(ValueError, match=r"minimum|size limit|grid"):
        _ = bot.reconcile_once(NOW)
    mock_child(client, "tunnel_market_order").assert_not_called()
    assert bot.journal.get("pending") is None


def test_side_filter_and_reentry_settings(
    bot: TunnelDemoBot,
) -> None:
    """Use the exported side and reentry settings without a reference signal."""
    bot.config = replace(bot.config, open_position_side="Short")
    assert "waiting" in bot.reconcile_once(NOW)
    bot.config = replace(
        bot.config, open_position_side="Both", allow_immediate_reentry=False
    )
    _ = bot.reconcile_once(NOW + timedelta(minutes=5))
    client = bot.client
    assert isinstance(client, Mock)
    mock_child(client, "order_by_link").return_value = {
        "orderStatus": "Rejected"
    }
    _ = bot.reconcile_once(NOW)
    assert bot.reconcile_once(NOW) == "reentry disabled"


@pytest.mark.parametrize(
    "change",
    [{"side": "Sell"}, {"size": "1000"}, {"positionIdx": 1}, {"leverage": "2"}],
)
def test_mismatched_fills_are_not_adopted(
    bot: TunnelDemoBot, change: dict[str, object]
) -> None:
    """Never modify a foreign side, hedge-mode position or larger exposure."""
    _ = bot.reconcile_once(NOW)
    client = bot.client
    assert isinstance(client, Mock)
    mock_child(client, "order_by_link").return_value = {"orderStatus": "Filled"}
    mock_child(client, "position").return_value = {
        "side": "Buy",
        "size": "10",
        "avgPrice": "100",
        **change,
    }
    with pytest.raises(RuntimeError, match="does not match"):
        _ = bot.reconcile_once(NOW)
    mock_child(client, "set_protection").assert_not_called()


def test_market_order_contains_protection_and_deterministic_identity(
    bot: TunnelDemoBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Send one demo POST with attached protection and the persisted link."""
    _ = bot.reconcile_once(NOW)
    content = bot.journal.get("pending")
    assert content is not None
    intent = pending_entry(content)
    request = Mock(return_value={"orderId": "accepted"})
    monkeypatch.setattr(BybitDemoClient, "_request", request)
    client = BybitDemoClient("key", "secret")
    assert client.tunnel_market_order(intent) == "accepted"
    request.assert_called_once_with(
        "POST",
        "/v5/order/create",
        {
            "category": "linear",
            "symbol": "BTCUSDT",
            "side": "Buy",
            "orderType": "Market",
            "qty": "10",
            "positionIdx": 0,
            "orderLinkId": intent.link,
            "stopLoss": "99.00",
            "takeProfit": "102.00",
            "tpslMode": "Full",
            "slOrderType": "Market",
            "tpOrderType": "Market",
        },
    )
    request.return_value = {}
    with pytest.raises(BybitDemoError, match="no order ID"):
        _ = client.tunnel_market_order(intent)
    client.tunnel_leverage()
    message = "API error [110043]"
    request.side_effect = BybitDemoError(message)
    client.tunnel_leverage()
    message = "API error [10001]"
    request.side_effect = BybitDemoError(message)
    with pytest.raises(BybitDemoError, match="10001"):
        client.tunnel_leverage()


@pytest.mark.parametrize("failed", [False, True])
def test_poll_loop_stops_without_resubmitting_or_removing_protection(
    bot: TunnelDemoBot, monkeypatch: pytest.MonkeyPatch, *, failed: bool
) -> None:
    """Pause on failures and leave protection active on shutdown."""
    message = "unavailable"
    reconcile = (
        Mock(side_effect=BybitDemoError(message))
        if failed
        else Mock(return_value="waiting")
    )
    monkeypatch.setattr(bot, "reconcile_once", reconcile)
    monkeypatch.setattr(signal, "signal", Mock())

    def stop_after_poll(_: int) -> None:
        bot.stop()

    monkeypatch.setattr(tunnel_executor, "sleep", stop_after_poll)
    bot.run(1)
    reconcile.assert_called_once()
    assert not bot.running
    client = bot.client
    assert isinstance(client, Mock)
    mock_child(client, "market_order").assert_not_called()


def test_demo_journal_requires_resume_before_creating_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A regular restart cannot replace durable order recovery state."""
    config_path = tmp_path / "config.json"
    _ = config_path.write_text(tunnel_config().to_json())
    state = tmp_path / "state.sqlite"
    state.touch()
    factory = Mock()
    monkeypatch.setattr(cli, "BybitDemoClient", factory)
    with pytest.raises(RuntimeError, match="use --resume"):
        cli.run_demo_command(
            config_path,
            tmp_path / "missing.env",
            paths=cli.DemoPaths(state=state),
        )
    factory.assert_not_called()
