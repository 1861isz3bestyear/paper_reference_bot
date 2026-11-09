# Copyright 2026 anchored-vwam-backtest contributors
"""Tests for reference bot test market."""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Self
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
from shared.testing_support import ignoring_arguments
from shared.validation import json_object, object_mapping
from tests.mock_support import call_kwargs, call_kwargs_at, mock_child

if TYPE_CHECKING:
    from pathlib import Path


DOWNLOADED_CANDLE_COUNT = 2


def candle_frame(
    times: tuple[str, ...] = ("2026-01-01T00:00:00Z",)
) -> pd.DataFrame:
    """Candle frame.

    Returns:
        The validated candle observations.

    """
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
    assert cache.load("BTCUSDT", "5m", start_ms=0, end_ms=10).empty
    frame = candle_frame(("2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z"))
    cache.upsert("BTCUSDT", "5m", frame)
    changed = frame.iloc[:1].copy()
    changed.loc[changed.index[0], "close"] = 9
    cache.upsert("BTCUSDT", "5m", changed)
    loaded = cache.load("BTCUSDT", "5m", start_ms=0, end_ms=2_000_000_000_000)
    assert loaded["close"].tolist() == [9, 1.5]
    cache.upsert("BTCUSDT", "5m", pd.DataFrame())


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ({"retCode": 1, "retMsg": "bad"}, "bad"),
        ({"retCode": 0, "result": {"list": []}}, "no valid"),
        ({"retCode": 0, "result": {"list": [{"lastPrice": "0"}]}}, "no valid"),
    ],
)
def test_fetch_live_price_validation(
    monkeypatch: pytest.MonkeyPatch, payload: object, expected_error: str
) -> None:
    """Verify fetch live price validation."""
    response = Mock()
    mock_child(response, "json").return_value = payload
    monkeypatch.setattr(
        "reference_bot.market.requests.get", Mock(return_value=response)
    )
    with pytest.raises(ValueError, match=expected_error):
        _ = fetch_live_price("BTCUSDT")


def test_fetch_live_price_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify fetch live price success."""
    response = Mock()
    mock_child(response, "json").return_value = {
        "retCode": 0,
        "result": {"list": [{"lastPrice": "12.5"}]},
    }
    get = Mock(return_value=response)
    monkeypatch.setattr("reference_bot.market.requests.get", get)
    assert fetch_live_price("BTCUSDT") == pytest.approx(12.5)
    mock_child(response, "raise_for_status").assert_called_once()


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
    responses: list[Mock] = []
    for page in pages:
        response = Mock()
        mock_child(response, "json").return_value = {
            "retCode": 0,
            "result": {"list": page},
        }
        responses.append(response)
    get = Mock(side_effect=responses)
    monkeypatch.setattr("reference_bot.market.requests.get", get)
    result = fetch_completed_linear_klines(
        "BTCUSDT", "5m", start_ms=0, end_ms=700_000
    )
    assert result["time"].tolist() == [pd.Timestamp("1970-01-01T00:05:00Z")]
    assert len(get.call_args_list) == DOWNLOADED_CANDLE_COUNT


def test_rest_klines_validation_and_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify rest klines validation and empty."""
    with pytest.raises(ValueError, match="does not support"):
        _ = fetch_completed_linear_klines("BTCUSDT", "2m", start_ms=0, end_ms=1)
    response = Mock()
    mock_child(response, "json").return_value = {
        "retCode": 0,
        "result": {"list": []},
    }
    monkeypatch.setattr(
        "reference_bot.market.requests.get", Mock(return_value=response)
    )
    assert fetch_completed_linear_klines(
        "BTCUSDT", "5m", start_ms=0, end_ms=1
    ).empty
    mock_child(response, "json").return_value = {
        "retCode": 2,
        "retMsg": "limited",
    }
    with pytest.raises(ValueError, match="limited"):
        _ = fetch_completed_linear_klines("BTCUSDT", "5m", start_ms=0, end_ms=1)


def test_mexc_rest_price_candles_and_reverse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify mexc rest price candles and reverse."""
    price_response = Mock()
    mock_child(price_response, "json").return_value = {
        "symbol": "SHIBUSDT",
        "price": "0.25",
    }
    monkeypatch.setattr(
        "reference_bot.market.requests.get", Mock(return_value=price_response)
    )
    assert fetch_mexc_live_price("SHIBUSDT") == pytest.approx(0.25)

    page = Mock()
    mock_child(page, "json").return_value = [
        [0, "2", "4", "1", "2.5", "3", 59_999, "7.5"]
    ]
    empty = Mock()
    mock_child(empty, "json").return_value = []
    get = Mock(side_effect=[page, empty])
    monkeypatch.setattr("reference_bot.market.requests.get", get)
    candles = fetch_completed_mexc_klines(
        "SHIBUSDT", "1m", start_ms=0, end_ms=120_000
    )
    reversed_candles = reverse_candles(candles)

    assert candles["close"].tolist() == [2.5]
    assert object_mapping(call_kwargs_at(get, 0)["params"])["interval"] == "1m"
    assert reversed_candles.iloc[0][
        ["open", "high", "low", "close"]
    ].tolist() == [0.5, 1.0, 0.25, 0.4]
    assert reversed_candles.iloc[0][
        ["volume", "quote_asset_volume"]
    ].tolist() == [7.5, 3.0]


def test_mexc_validation_and_rest_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify mexc validation and rest feed."""
    with pytest.raises(ValueError, match="does not support"):
        _ = fetch_completed_mexc_klines("SHIBUSDT", "2m", start_ms=0, end_ms=1)
    response = Mock()
    mock_child(response, "json").return_value = {"msg": "bad symbol"}
    monkeypatch.setattr(
        "reference_bot.market.requests.get", Mock(return_value=response)
    )
    with pytest.raises(ValueError, match="bad symbol"):
        _ = fetch_completed_mexc_klines("BAD", "1m", start_ms=0, end_ms=1)
    feed = RestMarketFeed()
    feed.start()
    assert not feed.health().connected
    feed.stop()


def test_mexc_taker_fee_comes_from_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify mexc taker fee comes from exchange."""
    response = Mock()
    mock_child(response, "json").return_value = {
        "symbols": [{"symbol": "SHIBUSDT", "takerCommission": "0.0004"}]
    }
    get = Mock(return_value=response)
    monkeypatch.setattr("reference_bot.market.requests.get", get)

    assert str(fetch_mexc_taker_fee_rate("SHIBUSDT")) == "0.0004"
    assert call_kwargs(get)["params"] == {"symbol": "SHIBUSDT"}


def websocket_message(*, confirm: bool = True) -> str:
    """Websocket message.

    Returns:
        The websocket message result.

    """
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
    received: list[pd.DataFrame] = []
    feed = BybitKlineWebSocket("BTCUSDT", "5m", received.append)
    feed.handle_message(json.dumps({"topic": "other", "data": []}))
    feed.handle_message(websocket_message(confirm=False))
    feed.handle_message(websocket_message(confirm=True))
    assert len(received) == 1
    assert pd.to_numeric(received[0]["close"]).astype(float).tolist()[
        0
    ] == pytest.approx(1.5)
    assert feed.health().last_completed_candle == "2026-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="does not support interval"):
        _ = BybitKlineWebSocket("BTCUSDT", "2m", received.append)


def test_websocket_worker_subscribes_and_stops_after_confirmed_candle() -> None:
    """Verify websocket worker subscribes and stops after confirmed candle."""
    sockets: list[Socket] = []
    received: list[pd.DataFrame] = []

    class Socket:
        """Represent socket."""

        def __init__(self) -> None:
            self.sent: dict[str, object] = {}

        def __enter__(self) -> Self:
            """Enter the response context.

            Returns:
                The enter result.

            """
            return self

        def __exit__(self, *_: object) -> None:
            """Release the response context."""
            return

        def send(self, value: str) -> None:
            """Send."""
            self.sent = json_object(value)

        @staticmethod
        def recv(timeout: float) -> str:
            """Recv.

            Returns:
                The recv result.

            """
            del timeout
            return websocket_message(confirm=True)

    confirmed = threading.Event()

    def on_candle(frame: pd.DataFrame) -> None:
        """On candle."""
        received.append(frame)
        feed.stop()
        confirmed.set()

    def connect(url: str, **kwargs: object) -> Socket:
        """Connect.

        Returns:
            The connect result.

        """
        del kwargs
        assert url == BYBIT_WEBSOCKET_URL
        socket = Socket()
        sockets.append(socket)
        return socket

    feed = BybitKlineWebSocket("BTCUSDT", "5m", on_candle, connect=connect)
    feed.start()
    assert confirmed.wait(timeout=5)
    feed.stop()
    assert sockets[0].sent == {"op": "subscribe", "args": ["kline.5.BTCUSDT"]}
    assert len(received) == 1
    assert not feed.health().connected


def test_websocket_start_is_idempotent_and_stop_joins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify websocket start is idempotent and stop joins."""
    feed = BybitKlineWebSocket(
        "BTCUSDT", "5m", ignoring_arguments(lambda: None)
    )
    worker = Mock()
    mock_child(worker, "is_alive").return_value = True
    monkeypatch.setattr(feed, "_thread", worker)
    feed.start()
    feed.stop()
    mock_child(worker, "join").assert_called_once_with(timeout=5)
