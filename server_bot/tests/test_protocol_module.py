# Copyright 2026 anchored-vwam-backtest contributors
"""Subsystem tests through real HTTP and filesystem boundaries."""

import json
import logging
import threading
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bybit_demo_bot.client import BybitDemoClient, BybitDemoError
from reference_bot.trading import LocalPosition
from shared.reference_target import publish_target

LOGGER = logging.getLogger(__name__)

pytestmark = pytest.mark.module


def test_http_client_signs_requests_and_reports_authentication_failure(
    exchange_http,
) -> None:
    """Verify http client signs requests and reports authentication failure."""
    client = BybitDemoClient(
        "test-key", "test-secret", base_url=exchange_http.url, timeout=2
    )
    if client.available_usdt() != Decimal(10000):
        pytest.fail("Expected client.available_usdt() == Decimal(10000)")
    if (client.position("BTCUSDT")) is not (None):
        pytest.fail("Expected client.position('BTCUSDT') is None")
    order = client.market_order(
        "BTCUSDT", "Buy", Decimal(2), order_link_id="module-order"
    )
    if order != "module-order":
        pytest.fail("Expected order == 'module-order'")
    if client.position("BTCUSDT")["size"] != "2":
        pytest.fail("Expected client.position('BTCUSDT')['size'] == '2'")
    if client.order_by_link("BTCUSDT", order)["orderStatus"] != "Filled":
        pytest.fail(
            "Expected client.order_by_link('BTCUSDT', order)['orderStatus'] =="
            " 'Filled'"
        )
    if client.has_open_orders("BTCUSDT"):
        pytest.fail("Expected not client.has_open_orders('BTCUSDT')")
    _ = client.market_order(
        "BTCUSDT",
        "Sell",
        Decimal(2),
        reduce_only=True,
        order_link_id="module-close",
    )
    if (client.position("BTCUSDT")) is not (None):
        pytest.fail("Expected client.position('BTCUSDT') is None")
    invalid = BybitDemoClient(
        "test-key", "wrong-secret", base_url=exchange_http.url, timeout=2
    )
    with pytest.raises(BybitDemoError, match="10004"):
        _ = invalid.available_usdt()


def test_atomic_target_publication_never_exposes_partial_json(system) -> None:
    """Verify atomic target publication never exposes partial json."""
    reference = system.producer()
    state = SimpleNamespace(
        launched_at=reference.state.launched_at,
        last_processed_candle="2026-01-01T00:01:00+00:00",
    )
    flat = LocalPosition("BTCUSDT", None, Decimal(0), None, None)
    opened = LocalPosition(
        "BTCUSDT", "Long", Decimal(10), 100.0, "2026-01-01T00:02:05+00:00"
    )
    publish_target(system.target, reference.config, state, position=flat)
    errors = []
    started = threading.Event()

    def writer():
        """Writer."""
        try:
            started.set()
            for i in range(100):
                publish_target(
                    system.target,
                    reference.config,
                    state,
                    position=opened if i % 2 else flat,
                )
        except Exception as exc:
            LOGGER.exception("Target publication failed")
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    if not (started.wait(2)):
        pytest.fail("Expected started.wait(2)")
    try:
        for _ in range(200):
            payload = json.loads(system.target.read_text())
            if ((
                payload["side"],
                payload["quantity"],
                payload["entry_price"],
            )) not in ([(None, "0", None), ("Long", "10", 100.0)]):
                pytest.fail(
                    "Expected (payload['side'], payload['quantity'],"
                    " payload['entry_price']) in [(None, '0', None), ('Long',"
                    " '10', 100.0)]"
                )
    finally:
        thread.join(timeout=5)
    if thread.is_alive():
        pytest.fail("Expected not thread.is_alive()")
    if errors:
        pytest.fail("Expected not errors")
    if system.target.with_suffix(".json.tmp").exists():
        pytest.fail(
            "Expected not system.target.with_suffix('.json.tmp').exists()"
        )
