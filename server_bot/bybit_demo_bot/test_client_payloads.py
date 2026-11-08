# Copyright 2026 anchored-vwam-backtest contributors
"""Reject malformed exchange payloads before consuming their contents."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bybit_demo_bot import client as client_module
from bybit_demo_bot.client import BybitDemoClient, BybitDemoError
from bybit_demo_bot.test_bybit_demo import Response

if TYPE_CHECKING:
    from urllib.request import Request


@pytest.mark.parametrize("payload", [None, [], "invalid", 1])
def test_non_object_responses_are_exchange_errors(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    """Reject a JSON scalar or array with a credential-safe exchange error."""

    def reply(_request: Request, *, timeout: float) -> Response:
        if timeout <= 0:
            pytest.fail("The exchange request must have a positive timeout.")
        return Response(payload)

    monkeypatch.setattr(client_module, "urlopen", reply)
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

    monkeypatch.setattr(client_module, "urlopen", reply)
    with pytest.raises(TypeError, match=r"Expected a JSON (object|array)"):
        _ = BybitDemoClient("test-key", "test-secret").instruments("BTCUSDT")
