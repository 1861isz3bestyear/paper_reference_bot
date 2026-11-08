import json
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest
import requests

from live_paper_bot.market import (
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
    return pd.DataFrame({
        "time": pd.to_datetime(list(times)), "open": 1.0, "high": 2.0, "low": 0.5,
        "close": 1.5, "volume": 10.0, "quote_asset_volume": 15.0, "number_of_trades": 0,
    })


def test_candle_cache_upserts_filters_and_replaces(tmp_path):
    cache = CandleCache(tmp_path / "candles.sqlite")
    assert cache.load("BTCUSDT", "5m", 0, 10).empty
    frame = candle_frame(("2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z"))
    cache.upsert("BTCUSDT", "5m", frame)
    changed = frame.iloc[:1].copy()
    changed.loc[changed.index[0], "close"] = 9
    cache.upsert("BTCUSDT", "5m", changed)
    loaded = cache.load("BTCUSDT", "5m", 0, 2_000_000_000_000)
    assert loaded["close"].tolist() == [9, 1.5]
    cache.upsert("BTCUSDT", "5m", pd.DataFrame())


@pytest.mark.parametrize("payload, expected_error", [
    ({"retCode": 1, "retMsg": "bad"}, "bad"),
    ({"retCode": 0, "result": {"list": []}}, "no valid"),
    ({"retCode": 0, "result": {"list": [{"lastPrice": "0"}]}}, "no valid"),
])
def test_fetch_live_price_validation(monkeypatch, payload, expected_error):
    response = Mock()
    response.json.return_value = payload
    monkeypatch.setattr("live_paper_bot.market.requests.get", Mock(return_value=response))
    with pytest.raises(ValueError, match=expected_error):
        fetch_live_price("BTCUSDT")


def test_fetch_live_price_success(monkeypatch):
    response = Mock()
    response.json.return_value = {"retCode": 0, "result": {"list": [{"lastPrice": "12.5"}]}}
    get = Mock(return_value=response)
    monkeypatch.setattr("live_paper_bot.market.requests.get", get)
    assert fetch_live_price("BTCUSDT") == 12.5
    response.raise_for_status.assert_called_once()


def test_rest_klines_paginate_deduplicate_and_drop_open_candle(monkeypatch):
    pages = [
        [["600000", "2", "3", "1", "2", "4", "8"], ["300000", "1", "2", ".5", "1.5", "3", "4.5"]],
        [["300000", "1", "2", ".5", "1.5", "3", "4.5"]],
        [],
    ]
    responses = []
    for page in pages:
        response = Mock()
        response.json.return_value = {"retCode": 0, "result": {"list": page}}
        responses.append(response)
    get = Mock(side_effect=responses)
    monkeypatch.setattr("live_paper_bot.market.requests.get", get)
    result = fetch_completed_linear_klines("BTCUSDT", "5m", 0, 700_000)
    assert result["time"].tolist() == [pd.Timestamp("1970-01-01T00:05:00Z")]
    assert len(get.call_args_list) == 2


def test_rest_klines_validation_and_empty(monkeypatch):
    with pytest.raises(ValueError, match="does not support"):
        fetch_completed_linear_klines("BTCUSDT", "2m", 0, 1)
    response = Mock()
    response.json.return_value = {"retCode": 0, "result": {"list": []}}
    monkeypatch.setattr("live_paper_bot.market.requests.get", Mock(return_value=response))
    assert fetch_completed_linear_klines("BTCUSDT", "5m", 0, 1).empty
    response.json.return_value = {"retCode": 2, "retMsg": "limited"}
    with pytest.raises(ValueError, match="limited"):
        fetch_completed_linear_klines("BTCUSDT", "5m", 0, 1)


def test_mexc_rest_price_candles_and_reverse(monkeypatch):
    price_response = Mock()
    price_response.json.return_value = {"symbol": "SHIBUSDT", "price": "0.25"}
    monkeypatch.setattr("live_paper_bot.market.requests.get", Mock(return_value=price_response))
    assert fetch_mexc_live_price("SHIBUSDT") == 0.25

    page = Mock()
    page.json.return_value = [[0, "2", "4", "1", "2.5", "3", 59_999, "7.5"]]
    empty = Mock()
    empty.json.return_value = []
    get = Mock(side_effect=[page, empty])
    monkeypatch.setattr("live_paper_bot.market.requests.get", get)
    candles = fetch_completed_mexc_klines("SHIBUSDT", "1m", 0, 120_000)
    reversed_candles = reverse_candles(candles)

    assert candles["close"].tolist() == [2.5]
    assert get.call_args_list[0].kwargs["params"]["interval"] == "1m"
    assert reversed_candles.iloc[0][["open", "high", "low", "close"]].tolist() == [0.5, 1.0, 0.25, 0.4]
    assert reversed_candles.iloc[0][["volume", "quote_asset_volume"]].tolist() == [7.5, 3.0]


def test_mexc_validation_and_rest_feed(monkeypatch):
    with pytest.raises(ValueError, match="does not support"):
        fetch_completed_mexc_klines("SHIBUSDT", "2m", 0, 1)
    response = Mock()
    response.json.return_value = {"msg": "bad symbol"}
    monkeypatch.setattr("live_paper_bot.market.requests.get", Mock(return_value=response))
    with pytest.raises(ValueError, match="bad symbol"):
        fetch_completed_mexc_klines("BAD", "1m", 0, 1)
    feed = RestMarketFeed()
    feed.start()
    assert not feed.health().connected
    feed.stop()


def test_mexc_taker_fee_comes_from_exchange(monkeypatch):
    response = Mock()
    response.json.return_value = {"symbols": [{"symbol": "SHIBUSDT", "takerCommission": "0.0004"}]}
    get = Mock(return_value=response)
    monkeypatch.setattr("live_paper_bot.market.requests.get", get)

    assert str(fetch_mexc_taker_fee_rate("SHIBUSDT")) == "0.0004"
    assert get.call_args.kwargs["params"] == {"symbol": "SHIBUSDT"}


def websocket_message(confirm=True):
    return json.dumps({"topic": "kline.5.BTCUSDT", "data": [{
        "start": 1_767_225_600_000, "open": "1", "high": "2", "low": ".5", "close": "1.5",
        "volume": "10", "turnover": "15", "confirm": confirm,
    }]})


def test_websocket_accepts_only_confirmed_matching_candles():
    received = []
    feed = BybitKlineWebSocket("BTCUSDT", "5m", received.append)
    feed._handle_message(json.dumps({"topic": "other", "data": []}))
    feed._handle_message(websocket_message(False))
    feed._handle_message(websocket_message(True))
    assert len(received) == 1
    assert received[0].iloc[0]["close"] == 1.5
    assert feed.health().last_completed_candle == "2026-01-01T00:00:00+00:00"
    with pytest.raises(ValueError):
        BybitKlineWebSocket("BTCUSDT", "2m", received.append)


def test_websocket_worker_subscribes_and_stops_after_confirmed_candle():
    sockets = []
    received = []

    class Socket:
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def send(self, value): self.sent = json.loads(value)
        def recv(self, timeout): return websocket_message(True)

    feed = None
    def on_candle(frame):
        received.append(frame)
        feed._stop.set()

    def connect(url, **kwargs):
        assert url == BYBIT_WEBSOCKET_URL
        socket = Socket()
        sockets.append(socket)
        return socket

    feed = BybitKlineWebSocket("BTCUSDT", "5m", on_candle, connect=connect)
    feed._run()
    assert sockets[0].sent == {"op": "subscribe", "args": ["kline.5.BTCUSDT"]}
    assert len(received) == 1
    assert not feed.health().connected


def test_websocket_start_is_idempotent_and_stop_joins(monkeypatch):
    feed = BybitKlineWebSocket("BTCUSDT", "5m", lambda _: None)
    worker = Mock()
    worker.is_alive.return_value = True
    feed._thread = worker
    feed.start()
    feed.stop()
    worker.join.assert_called_once_with(timeout=5)
