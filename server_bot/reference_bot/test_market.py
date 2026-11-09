# Copyright 2026 anchored-vwam-backtest contributors
"""Tests for reference bot test market."""

import json
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest

from reference_bot.market import (
    BYBIT_WEBSOCKET_URL,
    BybitKlineWebSocket,
    CandleCache,
    RestMarketFeed,
    fetch_completed_linear_klines,
    fetch_completed_mexc_klines,
    fetch_live_price,
    fetch_mexc_live_price,
    fetch_mexc_taker_fee_rate,
    reverse_candles,
)


def candle_frame(times=("2026-01-01T00:00:00Z",)):
    """Candle frame."""
    return pd.DataFrame({
        "time": pd.to_datetime(list(times)),
        "open": 1.0,
        "high": 2.0,
        "low": 0.5,
        "close": 1.5,
        "volume": 10.0,
        "quote_asset_volume": 15.0,
        "number_of_trades": 0,
    })


def test_candle_cache_upserts_filters_and_replaces(tmp_path: Path) -> None:
    """Verify candle cache upserts filters and replaces."""
    cache = CandleCache(tmp_path / "candles.sqlite")
    if not (cache.load("BTCUSDT", "5m", start_ms=0, end_ms=10).empty):
        pytest.fail("Expected cache.load('BTCUSDT', '5m', 0, 10).empty")
    frame = candle_frame(("2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z"))
    cache.upsert("BTCUSDT", "5m", frame)
    changed = frame.iloc[:1].copy()
    changed.loc[changed.index[0], "close"] = 9
    cache.upsert("BTCUSDT", "5m", changed)
    loaded = cache.load("BTCUSDT", "5m", start_ms=0, end_ms=2_000_000_000_000)
    if loaded["close"].tolist() != [9, 1.5]:
        pytest.fail("Expected loaded['close'].tolist() == [9, 1.5]")
    cache.upsert("BTCUSDT", "5m", pd.DataFrame())


@pytest.mark.parametrize(
    "payload, expected_error",
    [
        ({"retCode": 1, "retMsg": "bad"}, "bad"),
        ({"retCode": 0, "result": {"list": []}}, "no valid"),
        ({"retCode": 0, "result": {"list": [{"lastPrice": "0"}]}}, "no valid"),
    ],
)
def test_fetch_live_price_validation(
    monkeypatch: pytest.MonkeyPatch, payload, expected_error
) -> None:
    """Verify fetch live price validation."""
    response = Mock()
    response.json.return_value = payload
    monkeypatch.setattr(
        "reference_bot.market.requests.get", Mock(return_value=response)
    )
    with pytest.raises(ValueError, match=expected_error):
        _ = fetch_live_price("BTCUSDT")


def test_fetch_live_price_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify fetch live price success."""
    response = Mock()
    response.json.return_value = {
        "retCode": 0,
        "result": {"list": [{"lastPrice": "12.5"}]},
    }
    get = Mock(return_value=response)
    monkeypatch.setattr("reference_bot.market.requests.get", get)
    if fetch_live_price("BTCUSDT") != 12.5:
        pytest.fail("Expected fetch_live_price('BTCUSDT') == 12.5")
    response.raise_for_status.assert_called_once()


def test_rest_klines_paginate_deduplicate_and_drop_open_candle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify rest klines paginate deduplicate and drop open candle."""
    pages = [
        [
            ["600000", "2", "3", "1", "2", "4", "8"],
            ["300000", "1", "2", ".5", "1.5", "3", "4.5"],
        ],
        [["300000", "1", "2", ".5", "1.5", "3", "4.5"]],
        [],
    ]
    responses = []
    for page in pages:
        response = Mock()
        response.json.return_value = {"retCode": 0, "result": {"list": page}}
        responses.append(response)
    get = Mock(side_effect=responses)
    monkeypatch.setattr("reference_bot.market.requests.get", get)
    result = fetch_completed_linear_klines(
        "BTCUSDT", "5m", start_ms=0, end_ms=700_000
    )
    if result["time"].tolist() != [pd.Timestamp("1970-01-01T00:05:00Z")]:
        pytest.fail(
            "Expected result['time'].tolist() =="
            " [pd.Timestamp('1970-01-01T00:05:00Z')]"
        )
    if len(get.call_args_list) != 2:
        pytest.fail("Expected len(get.call_args_list) == 2")


def test_rest_klines_validation_and_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify rest klines validation and empty."""
    with pytest.raises(ValueError, match="does not support"):
        _ = fetch_completed_linear_klines("BTCUSDT", "2m", start_ms=0, end_ms=1)
    response = Mock()
    response.json.return_value = {"retCode": 0, "result": {"list": []}}
    monkeypatch.setattr(
        "reference_bot.market.requests.get", Mock(return_value=response)
    )
    if not (
        fetch_completed_linear_klines(
            "BTCUSDT", "5m", start_ms=0, end_ms=1
        ).empty
    ):
        pytest.fail(
            "Expected fetch_completed_linear_klines('BTCUSDT', '5m', 0,"
            " 1).empty"
        )
    response.json.return_value = {"retCode": 2, "retMsg": "limited"}
    with pytest.raises(ValueError, match="limited"):
        _ = fetch_completed_linear_klines("BTCUSDT", "5m", start_ms=0, end_ms=1)


def test_mexc_rest_price_candles_and_reverse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify mexc rest price candles and reverse."""
    price_response = Mock()
    price_response.json.return_value = {"symbol": "SHIBUSDT", "price": "0.25"}
    monkeypatch.setattr(
        "reference_bot.market.requests.get", Mock(return_value=price_response)
    )
    if fetch_mexc_live_price("SHIBUSDT") != 0.25:
        pytest.fail("Expected fetch_mexc_live_price('SHIBUSDT') == 0.25")

    page = Mock()
    page.json.return_value = [[0, "2", "4", "1", "2.5", "3", 59_999, "7.5"]]
    empty = Mock()
    empty.json.return_value = []
    get = Mock(side_effect=[page, empty])
    monkeypatch.setattr("reference_bot.market.requests.get", get)
    candles = fetch_completed_mexc_klines(
        "SHIBUSDT", "1m", start_ms=0, end_ms=120_000
    )
    reversed_candles = reverse_candles(candles)

    if candles["close"].tolist() != [2.5]:
        pytest.fail("Expected candles['close'].tolist() == [2.5]")
    if get.call_args_list[0].kwargs["params"]["interval"] != "1m":
        pytest.fail(
            "Expected get.call_args_list[0].kwargs['params']['interval'] =="
            " '1m'"
        )
    if reversed_candles.iloc[0][["open", "high", "low", "close"]].tolist() != [
        0.5,
        1.0,
        0.25,
        0.4,
    ]:
        pytest.fail(
            "Expected reversed_candles.iloc[0][['open', 'high', 'low',"
            " 'close']].tolist() == [0.5, 1.0, 0.25, 0.4]"
        )
    if reversed_candles.iloc[0][["volume", "quote_asset_volume"]].tolist() != [
        7.5,
        3.0,
    ]:
        pytest.fail(
            "Expected reversed_candles.iloc[0][['volume',"
            " 'quote_asset_volume']].tolist() == [7.5, 3.0]"
        )


def test_mexc_validation_and_rest_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify mexc validation and rest feed."""
    with pytest.raises(ValueError, match="does not support"):
        _ = fetch_completed_mexc_klines("SHIBUSDT", "2m", start_ms=0, end_ms=1)
    response = Mock()
    response.json.return_value = {"msg": "bad symbol"}
    monkeypatch.setattr(
        "reference_bot.market.requests.get", Mock(return_value=response)
    )
    with pytest.raises(ValueError, match="bad symbol"):
        _ = fetch_completed_mexc_klines("BAD", "1m", start_ms=0, end_ms=1)
    feed = RestMarketFeed()
    feed.start()
    if feed.health().connected:
        pytest.fail("Expected not feed.health().connected")
    feed.stop()


def test_mexc_taker_fee_comes_from_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify mexc taker fee comes from exchange."""
    response = Mock()
    response.json.return_value = {
        "symbols": [{"symbol": "SHIBUSDT", "takerCommission": "0.0004"}]
    }
    get = Mock(return_value=response)
    monkeypatch.setattr("reference_bot.market.requests.get", get)

    if str(fetch_mexc_taker_fee_rate("SHIBUSDT")) != "0.0004":
        pytest.fail(
            "Expected str(fetch_mexc_taker_fee_rate('SHIBUSDT')) == '0.0004'"
        )
    if get.call_args.kwargs["params"] != {"symbol": "SHIBUSDT"}:
        pytest.fail(
            "Expected get.call_args.kwargs['params'] == {'symbol': 'SHIBUSDT'}"
        )


def websocket_message(confirm=True):
    """Websocket message."""
    return json.dumps({
        "topic": "kline.5.BTCUSDT",
        "data": [{
            "start": 1_767_225_600_000,
            "open": "1",
            "high": "2",
            "low": ".5",
            "close": "1.5",
            "volume": "10",
            "turnover": "15",
            "confirm": confirm,
        }],
    })


def test_websocket_accepts_only_confirmed_matching_candles() -> None:
    """Verify websocket accepts only confirmed matching candles."""
    received = []
    feed = BybitKlineWebSocket("BTCUSDT", "5m", received.append)
    feed._handle_message(json.dumps({"topic": "other", "data": []}))
    feed._handle_message(websocket_message(False))
    feed._handle_message(websocket_message(True))
    if len(received) != 1:
        pytest.fail("Expected len(received) == 1")
    if received[0].iloc[0]["close"] != 1.5:
        pytest.fail("Expected received[0].iloc[0]['close'] == 1.5")
    if feed.health().last_completed_candle != "2026-01-01T00:00:00+00:00":
        pytest.fail(
            "Expected feed.health().last_completed_candle =="
            " '2026-01-01T00:00:00+00:00'"
        )
    with pytest.raises(ValueError):
        _ = BybitKlineWebSocket("BTCUSDT", "2m", received.append)


def test_websocket_worker_subscribes_and_stops_after_confirmed_candle() -> None:
    """Verify websocket worker subscribes and stops after confirmed candle."""
    sockets = []
    received = []

    class Socket:
        """Represent socket."""

        def __enter__(self):
            """Enter the response context."""
            return self

        def __exit__(self, *_):
            """Release the response context."""
            return

        def send(self, value):
            """Send."""
            self.sent = json.loads(value)

        def recv(self, timeout):
            """Recv."""
            return websocket_message(True)

    feed = None

    def on_candle(frame):
        """On candle."""
        received.append(frame)
        feed._stop.set()

    def connect(url, **kwargs):
        """Connect."""
        if url != BYBIT_WEBSOCKET_URL:
            pytest.fail("Expected url == BYBIT_WEBSOCKET_URL")
        socket = Socket()
        sockets.append(socket)
        return socket

    feed = BybitKlineWebSocket("BTCUSDT", "5m", on_candle, connect=connect)
    feed._run()
    if sockets[0].sent != {"op": "subscribe", "args": ["kline.5.BTCUSDT"]}:
        pytest.fail(
            "Expected sockets[0].sent == {'op': 'subscribe', 'args':"
            " ['kline.5.BTCUSDT']}"
        )
    if len(received) != 1:
        pytest.fail("Expected len(received) == 1")
    if feed.health().connected:
        pytest.fail("Expected not feed.health().connected")


def test_websocket_start_is_idempotent_and_stop_joins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify websocket start is idempotent and stop joins."""
    feed = BybitKlineWebSocket("BTCUSDT", "5m", lambda _: None)
    worker = Mock()
    worker.is_alive.return_value = True
    feed._thread = worker
    feed.start()
    feed.stop()
    worker.join.assert_called_once_with(timeout=5)
