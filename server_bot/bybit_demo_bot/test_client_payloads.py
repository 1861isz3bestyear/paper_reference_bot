# Copyright 2026 anchored-vwam-backtest contributors
"""Reject malformed exchange payloads before consuming their contents."""

from __future__ import annotations

from unittest.mock import Mock
from urllib.request import Request

import pytest

from bybit_demo_bot import client as client_module
from bybit_demo_bot.client import BybitDemoClient, BybitDemoError, open_http
from bybit_demo_bot.test_bybit_demo import Response


@pytest.mark.parametrize("payload", [None, [], "invalid", 1])
def test_non_object_responses_are_exchange_errors(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    """Reject a JSON scalar or array with a credential-safe exchange error."""

    def reply(_request: Request, *, timeout: float) -> Response:
        if timeout <= 0:
            pytest.fail("The exchange request must have a positive timeout.")
        return Response(payload)

    monkeypatch.setattr(client_module, "open_http", reply)
    with pytest.raises(BybitDemoError, match="Expected a JSON object"):
        _ = BybitDemoClient("test-key", "test-secret").last_price("BTCUSDT")


@pytest.mark.parametrize("rows", [{}, [1], [None], ["invalid"]])
def test_instrument_rows_require_objects(
    monkeypatch: pytest.MonkeyPatch, rows: object
) -> None:
    """Reject malformed instrument lists before indexing exchange metadata."""

    def reply(_request: Request, *, timeout: float) -> Response:
        if timeout <= 0:
            pytest.fail("The exchange request must have a positive timeout.")
        return Response({"retCode": 0, "result": {"list": rows}})

    monkeypatch.setattr(client_module, "open_http", reply)
    with pytest.raises(TypeError, match=r"Expected a JSON (object|array)"):
        _ = BybitDemoClient("test-key", "test-secret").instruments("BTCUSDT")


@pytest.mark.parametrize("url", ["file:///tmp/exchange", "ftp://exchange"])
def test_transport_rejects_changed_request_schemes(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a request whose URL changes before transport opens it."""
    request = Request("https://exchange.invalid")
    request.full_url = url
    opener = Mock()
    monkeypatch.setattr(client_module, "build_opener", opener)
    with pytest.raises(ValueError, match="HTTP or HTTPS"):
        _ = open_http(request, timeout=1)
    opener.assert_not_called()
