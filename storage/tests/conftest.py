# Copyright 2026 avwap-workspace-tests contributors
"""Use deterministic receipt times and isolated databases."""

from datetime import UTC, datetime

import pytest

from market_storage.models import Observation, Source

STAMP = datetime(2026, 11, 10, 12, tzinfo=UTC)


@pytest.fixture
def observation() -> Observation:
    """Provide a valid observation independent of the current clock.

    Returns:
        One exact successful public response.

    """
    return Observation(
        asset="BTC",
        source=Source.SPOT,
        requested=STAMP,
        available=STAMP,
        payload='{"result":{"index_price":100000}}',
        collection="first",
    )
