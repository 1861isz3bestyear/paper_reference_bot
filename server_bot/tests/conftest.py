# Copyright 2026 anchored-vwam-backtest contributors
"""Simulate the Bybit protocol using isolated local accounts."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, ClassVar, final, override

from bybit_demo_bot import cli as demo
from bybit_demo_bot import replica
from bybit_demo_bot import state as execution_state
from bybit_demo_bot.client import BybitDemoClient
from reference_bot import cli as reference
from reference_bot import market, trading
from reference_bot.config import PaperBotConfig
from shared import reference_target
from shared.testing_support import ignoring_arguments
from shared.validation import integer, json_object, string

if TYPE_CHECKING:
    import socket
    from collections.abc import Callable, Generator
    from pathlib import Path
    from socketserver import BaseServer

    from bybit_demo_bot.hedge import HedgeReplicaBot
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import pytest


@dataclass(kw_only=True)
class ExchangeState:
    """Mutable exchange simulator data shared by HTTP requests and scenarios."""

    price: str = "100"
    hedge_mode: bool = False
    hedge_positions: dict[int, dict[str, object]] = field(default_factory=dict)
    position: dict[str, object] | None = None
    orders: dict[str, dict[str, object]] = field(default_factory=dict)
    submissions: list[dict[str, object]] = field(default_factory=list)
    requests: list[tuple[str, str, dict[str, object], bool]] = field(
        default_factory=list
    )
    lose_next_response: bool = False
    defer_fills: bool = False
    fail_kline: bool = False
    candles: list[list[str]] = field(default_factory=list)
    url: str = ""

    def fill(self, link: str) -> None:
        """Apply one accepted order to the simulated exchange positions."""
        order = self.orders[link]
        order["orderStatus"] = "Filled"
        if order.get("positionIdx"):
            index = integer(order["positionIdx"])
            if order.get("reduceOnly"):
                _ = self.hedge_positions.pop(index, None)
            else:
                self.hedge_positions[index] = {
                    "symbol": order["symbol"],
                    "side": order["side"],
                    "size": order["qty"],
                    "avgPrice": order.get("price", self.price),
                    "positionIdx": index,
                    "stopLoss": order.get("stopLoss", "0"),
                    "takeProfit": order.get("takeProfit", "0"),
                }
        elif order["reduceOnly"]:
            self.position = None
        else:
            self.position = {
                "symbol": order["symbol"],
                "side": order["side"],
                "size": order["qty"],
                "avgPrice": self.price,
                "stopLoss": "0",
                "takeProfit": "0",
            }


class Clock(datetime):
    """Controlled UTC clock for producers and exchange followers."""

    value: ClassVar[datetime] = datetime(2026, 1, 1, 0, 2, 5, tzinfo=UTC)

    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None) -> Clock:
        """Read the controlled instant in the requested timezone.

        Returns:
            The scenario clock as a datetime-compatible value.

        """
        instant = (
            cls.value.astimezone(tz) if tz else cls.value.replace(tzinfo=None)
        )
        return cls.fromisoformat(instant.isoformat())


@dataclass(kw_only=True)
class TradingSystem:
    """Keep real persistence and HTTP boundaries for a bot scenario."""

    producer: Callable[[], reference.PaperBot]
    consumer: Callable[[], replica.ReferenceReplicaBot | HedgeReplicaBot]
    clock: type[Clock]
    target: Path
    account: Path
    state_path: Path
    client: BybitDemoClient
    exchange: ExchangeState
    demo_path: Path
    cfg: PaperBotConfig


@final
class Handler(BaseHTTPRequestHandler):
    """Serve an isolated exchange state through real HTTP boundaries."""

    def __init__(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
        server: BaseServer,
        *,
        exchange: ExchangeState,
    ) -> None:
        """Bind each connection to its fixture's exchange state."""
        self.exchange: ExchangeState = exchange
        super().__init__(request, client_address, server)

    @override
    def log_message(self, *_args: object, **_kwargs: object) -> None:
        """Log message."""

    def respond(
        self,
        result: object = None,
        *,
        code: int = 0,
        message: str = "OK",
        status: int = 200,
    ) -> None:
        """Respond."""
        data = json.dumps(
            {"retCode": code, "retMsg": message, "result": result or {}}
        ).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        _ = self.wfile.write(data)

    def handle_request(self) -> None:
        """Handle request."""
        url = urlsplit(self.path)
        body = self.rfile.read(
            int(self.headers.get("Content-Length", "0"))
        ).decode()
        values: dict[str, object] = (
            json_object(body)
            if body
            else {k: v[0] for k, v in parse_qs(url.query).items()}
        )
        private = url.path.startswith(
            ("/v5/account/", "/v5/position/", "/v5/order/")
        )
        if private:
            timestamp = self.headers.get("X-BAPI-TIMESTAMP", "")
            recv = self.headers.get("X-BAPI-RECV-WINDOW", "")
            signed = f"{timestamp}test-key{recv}{body or url.query}"
            expected = hmac.new(
                b"test-secret", signed.encode(), hashlib.sha256
            ).hexdigest()
            if (
                self.headers.get("X-BAPI-API-KEY") != "test-key"
                or self.headers.get("X-BAPI-SIGN") != expected
            ):
                self.respond(code=10004, message="bad test signature")
                return
        self.exchange.requests.append((self.command, url.path, values, private))
        if url.path.startswith("/v5/market/"):
            self.handle_market_request(url.path, values)
        elif url.path.startswith("/v5/order/"):
            self.handle_order_request(url.path, values)
        else:
            self.handle_account_request(url.path, values)

    def handle_market_request(
        self, path: str, values: dict[str, object]
    ) -> None:
        """Serve deterministic candle and instrument data."""
        if path == "/v5/market/kline":
            if self.exchange.fail_kline:
                self.respond(status=503)
                return
            rows = [
                r
                for r in self.exchange.candles
                if int(str(values["start"]))
                <= int(r[0])
                <= int(str(values["end"]))
            ]
            self.respond(
                {"list": sorted(rows, key=lambda r: int(r[0]), reverse=True)}
            )
            return
        if path == "/v5/market/mark-price-kline":
            rows = [
                r[:5]
                for r in self.exchange.candles
                if int(str(values["start"]))
                <= int(r[0])
                <= int(str(values["end"]))
            ]
            result = {
                "list": sorted(rows, key=lambda r: int(r[0]), reverse=True)
            }
        elif path == "/v5/market/funding/history":
            start = int(self.exchange.candles[0][0])
            times = [start - 480 * 60000, start]
            result = {
                "list": [
                    {"fundingRateTimestamp": str(t), "fundingRate": ".0001"}
                    for t in times
                    if int(str(values["startTime"]))
                    <= t
                    <= int(str(values["endTime"]))
                ]
            }
        elif path == "/v5/market/risk-limit":
            result = {
                "list": [{
                    "riskLimitValue": "1000000",
                    "maintenanceMargin": ".005",
                    "mmDeduction": "",
                    "maxLeverage": "100",
                }]
            }
        elif path == "/v5/market/tickers":
            result = {"list": [{"lastPrice": self.exchange.price}]}
        elif path == "/v5/market/instruments-info":
            result = {
                "list": [{
                    "contractType": "LinearPerpetual",
                    "settleCoin": "USDT",
                    "fundingInterval": 480,
                    "leverageFilter": {
                        "maxLeverage": "100",
                        "leverageStep": "1",
                    },
                    "priceFilter": {"tickSize": "0.01"},
                    "lotSizeFilter": {
                        "qtyStep": "0.001",
                        "minOrderQty": "0.001",
                        "minNotionalValue": "1",
                        "maxMktOrderQty": "10000",
                    },
                }]
            }
        else:
            self.respond(code=10001, message="unhandled test endpoint")
            return
        self.respond(result)

    def handle_account_request(
        self, path: str, values: dict[str, object]
    ) -> None:
        """Serve signed account balances, fees and position state."""
        del values
        if path == "/v5/account/fee-rate":
            result = {"list": [{"takerFeeRate": "0.001"}]}
        elif path == "/v5/account/wallet-balance":
            result = {
                "list": [{"coin": [{"coin": "USDT", "walletBalance": "10000"}]}]
            }
        elif path == "/v5/position/list":
            result = {
                "list": (
                    list(self.exchange.hedge_positions.values())
                    if self.exchange.hedge_mode
                    else (
                        [self.exchange.position]
                        if self.exchange.position
                        else []
                    )
                )
            }
        elif path == "/v5/position/trading-stop":
            result = {}
        else:
            self.respond(code=10001, message="unhandled test endpoint")
            return
        self.respond(result)

    def handle_order_request(
        self, path: str, values: dict[str, object]
    ) -> None:
        """Apply simulated order submission, observation and cancellation."""
        if path == "/v5/order/create":
            link = string(
                values.get(
                    "orderLinkId", f"emergency-{len(self.exchange.submissions)}"
                )
            )
            if link in self.exchange.orders:
                self.respond(code=10014, message="duplicate order")
                return
            self.exchange.submissions.append(values)
            self.exchange.orders[link] = dict(
                values, orderId=link, orderStatus="New"
            )
            if not self.exchange.defer_fills:
                self.exchange.fill(link)
            if self.exchange.lose_next_response:
                self.exchange.lose_next_response = False
                self.respond(status=503)
                return  # Accepted/fill persisted, response unavailable.
            result = {"orderId": link}
        elif path in {"/v5/order/realtime", "/v5/order/history"}:
            link = string(values.get("orderLinkId", ""))
            rows = (
                [self.exchange.orders[link]]
                if link in self.exchange.orders
                else []
            )
            if not link:
                rows = [
                    o
                    for o in self.exchange.orders.values()
                    if o["orderStatus"] == "New"
                ]
            result = {"list": rows}
        elif path == "/v5/order/cancel":
            self.exchange.orders[string(values["orderLinkId"])][
                "orderStatus"
            ] = "Cancelled"
            result = {}
        else:
            self.respond(code=10001, message="unhandled test endpoint")
            return
        self.respond(result)

    def do_GET(self) -> None:
        """Serve the HTTP handler's required GET entry point."""
        self.handle_request()

    def do_POST(self) -> None:
        """Serve the HTTP handler's required POST entry point."""
        self.handle_request()


@pytest.fixture
def exchange_http() -> Generator[ExchangeState]:
    """Serve an isolated exchange over HTTP.

    Yields:
        The mutable exchange simulation with its listening URL.

    """
    start = int(pd.Timestamp("2026-01-01", tz="UTC").timestamp() * 1000)
    state = ExchangeState(
        price="100",
        hedge_mode=False,
        hedge_positions={},
        position=None,
        orders={},
        submissions=[],
        requests=[],
        lose_next_response=False,
        defer_fills=False,
        fail_kline=False,
        candles=[
            [
                str(start + i * 60000),
                str(o),
                str(h),
                str(low),
                str(c),
                "10",
                str(c * 10),
            ]
            for i, (o, h, low, c) in enumerate([
                (100, 100, 100, 100),
                (100, 101, 100, 101),
                (102, 102, 102, 102),
            ])
        ],
    )

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(Handler, exchange=state)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def system(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exchange_http: ExchangeState,
) -> TradingSystem:
    """System.

    Returns:
        The system result.

    """
    Clock.value = datetime(2026, 1, 1, 0, 2, 5, tzinfo=UTC)
    for module in (reference, trading, replica, reference_target):
        monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(
        market, "BYBIT_KLINES_URL", exchange_http.url + "/v5/market/kline"
    )
    monkeypatch.setattr(
        market, "BYBIT_TICKERS_URL", exchange_http.url + "/v5/market/tickers"
    )
    monkeypatch.setattr(
        trading,
        "BYBIT_FEE_RATE_URL",
        exchange_http.url + "/v5/account/fee-rate",
    )
    monkeypatch.setattr(
        reference, "load_bybit_credentials", lambda: ("test-key", "test-secret")
    )
    account = tmp_path / "reference_account.json"
    monkeypatch.setattr(
        reference, "LocalPaperTrader", lambda: trading.LocalPaperTrader(account)
    )
    monkeypatch.setattr(
        execution_state, "STATE_FILE", tmp_path / "demo_state.json"
    )
    cfg = PaperBotConfig(
        strategy_mode="VWAP band mean reversion",
        timeframe="1m",
        trend=True,
        open_order_vwap_sigma=1,
        close_order_vwap_sigma=2,
        initial_capital=1000,
        stop_loss_pct=0,
        allow_immediate_reentry=True,
        vwap_anchor_reset_weeks=2,
        data_source="Bybit REST",
        ticker="BTC_USDT",
        open_position_side="Long",
        minimum_order_size=0.01,
    )
    config_path = tmp_path / "config.json"
    cfg.save(config_path)
    state_path = tmp_path / "reference_state.json"
    reference.BotState("2026-01-01T00:00:00+00:00").save(state_path)

    def producer() -> reference.PaperBot:
        """Create the reference producer.

        Returns:
            The producer result.

        """
        bot = reference.PaperBot(
            config_path, state_path, tmp_path / "candles.sqlite"
        )
        # The external websocket is disabled; production REST ingestion is used.
        bot.market_feed = market.RestMarketFeed()
        monkeypatch.setattr(
            bot, "_wait", ignoring_arguments(bot.stop)
        )  # Execute exactly one production loop iteration.
        return bot

    client = BybitDemoClient(
        "test-key", "test-secret", base_url=exchange_http.url, timeout=2
    )
    target = tmp_path / "reference_target.json"

    def consumer() -> replica.ReferenceReplicaBot:
        """Consumer.

        Returns:
            The consumer result.

        """
        return replica.ReferenceReplicaBot(
            cfg,
            client,
            demo.DemoState.load_or_create(resume=True),
            target_path=target,
        )

    return TradingSystem(
        producer=producer,
        consumer=consumer,
        clock=Clock,
        target=target,
        account=account,
        state_path=state_path,
        client=client,
        exchange=exchange_http,
        demo_path=execution_state.STATE_FILE,
        cfg=cfg,
    )
