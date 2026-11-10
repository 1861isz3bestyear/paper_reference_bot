# Copyright 2026 anchored-vwam-backtest contributors
"""Verify collection cadence across process restarts and exchange failures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from io import BytesIO
from types import SimpleNamespace
from typing import TYPE_CHECKING, override

import pytest
import requests
from market_storage.archive import Archive
from market_storage.inputs import load_inputs
from market_storage.models import Source, Window

from server_bot import tunnel_archive

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

STAMP = datetime(2026, 11, 10, 12, tzinfo=UTC)


class Clock(datetime):
    """Supply the same UTC clock to scheduling and request receipts."""

    current: datetime = STAMP

    @classmethod
    @override
    def now(cls, tz: tzinfo | None = None) -> datetime:
        """Return deterministic aware request/receipt times.

        Returns:
            The fixture-controlled timestamp.

        """
        return cls.current.astimezone(tz or UTC)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[Clock]]:
    """Keep source intervals deterministic and remove only exchange pacing.

    Yields:
        The clock whose current value each scenario advances.

    """
    Clock.current = STAMP
    monkeypatch.setattr(tunnel_archive, "datetime", Clock)

    def sleep(_seconds: float) -> None:
        pass

    monkeypatch.setattr(tunnel_archive, "time", SimpleNamespace(sleep=sleep))
    yield Clock
    Clock.current = STAMP


def test_request_contract_has_no_ohlcv_and_uses_five_minute_oi() -> None:
    """Unit-check feed configuration before network or storage is involved."""
    sources = tunnel_archive.sources("BTC")
    assert not any("kline" in source.url for source in sources)
    assert (
        next(source.params for source in sources if source.name == "oi")[
            "intervalTime"
        ]
        == "5min"
    )
    with pytest.raises(ValueError, match="Retention"):
        _ = tunnel_archive.CollectionSettings(retention=timedelta(0))


@pytest.mark.module
@pytest.mark.usefixtures("clock")
def test_restart_respects_independent_source_buckets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only fast feeds repeat at five minutes; lending repeats at the hour."""
    seen: list[str] = []
    session = requests.Session()

    def get(url: str, **_kwargs: object) -> requests.Response:
        seen.append(url)
        response = requests.Response()
        response.status_code = 200
        response.raw = BytesIO(b'{"result":{},"code":"00000","data":[]}')
        return response

    monkeypatch.setattr(session, "get", get)
    path = tmp_path / "archive.sqlite"
    assert tunnel_archive.collect_due(path, session) == 0
    first, fast = 9, 6
    assert len(seen) == first
    assert tunnel_archive.collect_due(path, session) == 0
    assert len(seen) == first
    Clock.current += timedelta(minutes=5)
    assert tunnel_archive.collect_due(path, session) == 0
    assert len(seen) == first + fast
    Clock.current = STAMP + timedelta(hours=1)
    assert tunnel_archive.collect_due(path, session) == 0
    assert len(seen) == first * 2 + fast
    rows = Archive(path=path).load(
        Window(asset="BTC", start=STAMP, end=Clock.current)
    )
    hourly_polls, fast_polls = 2, 3
    assert (
        sum(row.source == Source.GROWTH_HOURLY for row in rows) == hourly_polls
    )
    assert sum(row.source == Source.OI for row in rows) == fast_polls


@pytest.mark.module
@pytest.mark.usefixtures("clock")
def test_failed_source_retries_without_repolling_successful_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retry failed feeds without repeating successful requests."""
    seen: list[str] = []
    session = requests.Session()

    def get(url: str, **_kwargs: object) -> requests.Response:
        seen.append(url)
        response = requests.Response()
        response.status_code = 503 if len(seen) == 1 else 200
        response.raw = BytesIO(b'{"result":{},"code":"00000","data":[]}')
        return response

    monkeypatch.setattr(session, "get", get)
    path = tmp_path / "archive.sqlite"
    assert tunnel_archive.collect_due(path, session) == 1
    assert tunnel_archive.collect_due(path, session) == 0
    expected_requests = 10
    assert len(seen) == expected_requests
    assert seen[-1] == seen[0]
    assert tunnel_archive.collect_due(path, session) == 0
    assert len(seen) == expected_requests

    window = Window(asset="BTC", start=STAMP, end=Clock.current)
    assert len(load_inputs(Archive(path=path), window).gex) == 1
