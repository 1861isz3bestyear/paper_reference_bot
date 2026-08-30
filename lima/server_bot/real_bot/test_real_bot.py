from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock
import pandas as pd
import pytest
from real_bot import cli
from real_bot.client import MEXCError, MEXCFuturesClient

def config() -> Mock:
    return Mock(data_source="MEXC REST", reverse_ticker=False, strategy_mode="VWAP band mean reversion", trend=True,
                ticker="SHIB_USDT", timeframe="1m", initial_capital=10.0, minimum_order_size=0.01, stop_loss_pct=0.4)

def bot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[cli.RealBot, Mock]:
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(cli, "REFERENCE_CANDLES", tmp_path / "candles.sqlite")
    client = Mock(); client.positions.return_value = []; client.available_usdt.return_value = Decimal("20")
    client.last_price.return_value = Decimal("0.000010")
    client.contract.return_value = {"contractSize": "1000", "priceUnit": "0.000000001", "minVol": 1, "volUnit": 1}
    instance = cli.RealBot(config(), client, leverage=1, open_type=1)
    monkeypatch.setattr(instance, "_latest_candle", lambda _: (pd.Timestamp("2026-08-29T12:00:00Z"), pd.Timestamp("2026-08-01T00:00:00Z")))
    monkeypatch.setattr(instance, "_bands", lambda *_: tuple(map(Decimal, ("0.000011", "0.000012", "0.000009", "0.000008"))))
    return instance, client

def test_waits_between_bands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path); assert instance.reconcile_once(180) is None
    client.submit_market_order.assert_not_called()

def test_crossing_submits_one_market_entry_and_persists_sltp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path); client.last_price.return_value = Decimal("0.000011")
    assert "awaiting fill" in instance.reconcile_once(180)
    call = client.submit_market_order.call_args.kwargs
    assert call["side"] == 1 and "stop_loss_price" not in call and "take_profit_price" not in call
    assert instance.state.entry_submission["stop_loss"] == "0.000010956"
    assert instance.state.entry_submission["take_profit"] == "0.000012"

def test_fill_installs_protection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path)
    instance.state.entry_submission = {"side": "Long", "stop_loss": "0.000010", "take_profit": "0.000012", "submitted_at": "2026-08-29T12:00:00+00:00"}
    client.positions.return_value = [{"positionId": 99, "holdVol": 5, "positionType": 1}]
    assert "confirmed" in instance.reconcile_once(180)
    client.set_position_protection.assert_called_once_with(99, stop_loss_price=Decimal("0.000010"), take_profit_price=Decimal("0.000012"))

def test_protection_failure_emergency_closes_and_halts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path)
    instance.state.entry_submission = {"side": "Long", "stop_loss": "0.000010", "take_profit": "0.000012", "submitted_at": "2026-08-29T12:00:00+00:00"}
    client.positions.return_value = [{"positionId": 99, "holdVol": 5, "positionType": 1}]
    client.set_position_protection.side_effect = RuntimeError("rejected")
    with pytest.raises(RuntimeError, match="emergency close submitted"): instance.reconcile_once(180)
    assert client.submit_market_order.call_args.kwargs["side"] == 4 and instance.state.halted_reason

def test_market_payload_contains_attached_sltp(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MEXCFuturesClient("key", "secret"); request = Mock(return_value=123); monkeypatch.setattr(client, "_request", request)
    client.submit_market_order(symbol="SHIB_USDT", side=1, volume=5, leverage=1, open_type=1, external_oid="entry-1",
        stop_loss_price=Decimal("0.9"), take_profit_price=Decimal("1.2"))
    payload = request.call_args.args[2]
    assert payload["type"] == 5 and payload["stopLossPrice"] == "0.9" and payload["takeProfitPrice"] == "1.2"

def test_single_entry_can_use_98_percent_margin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path); client.available_usdt.return_value = Decimal("9")
    client.last_price.return_value = Decimal("0.000011"); instance.reconcile_once(180)
    assert client.submit_market_order.call_args.kwargs["volume"] == 801

def test_uncertain_entry_outcome_halts_without_resubmitting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path)
    instance.state.entry_submission = {"side": "Long", "stop_loss": "1", "take_profit": "2",
        "submitted_at": (datetime.now(timezone.utc) - timedelta(seconds=31)).isoformat()}
    with pytest.raises(RuntimeError, match="uncertain"): instance.reconcile_once(180)
    client.submit_market_order.assert_not_called()
    assert "uncertain" in instance.state.halted_reason

def test_structured_entry_rejection_halts_without_uncertain_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path); client.last_price.return_value = Decimal("0.000011")
    client.submit_market_order.side_effect = MEXCError("MEXC POST /order API error: rejected")
    with pytest.raises(MEXCError, match="rejected"): instance.reconcile_once(180)
    assert instance.state.entry_submission is None
    assert "market entry rejected" in instance.state.halted_reason

def test_transport_failure_keeps_submission_for_reconciliation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance, client = bot(monkeypatch, tmp_path); client.last_price.return_value = Decimal("0.000011")
    client.submit_market_order.side_effect = MEXCError("MEXC request failed: timed out")
    with pytest.raises(MEXCError, match="timed out"): instance.reconcile_once(180)
    assert instance.state.entry_submission["side"] == "Long"
    assert instance.state.halted_reason is None
