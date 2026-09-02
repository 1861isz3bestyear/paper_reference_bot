from __future__ import annotations

from dataclasses import dataclass

from bybit_demo_bot.client import BybitDemoClient, BybitDemoError


BybitError = BybitDemoError


@dataclass
class BybitClient(BybitDemoClient):
    """Authenticated Bybit mainnet client using the shared V5 implementation."""

    base_url: str = "https://api.bybit.com"
    environment_name: str = "Bybit mainnet"
    user_agent: str = "server-bot-bybit-mainnet/1.0"
