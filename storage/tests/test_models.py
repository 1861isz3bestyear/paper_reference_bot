# Copyright 2026 avwap-workspace-tests contributors
"""Unit validation of storage domains and safe SSH command operands."""

import shlex
from dataclasses import replace
from datetime import timedelta

import pytest

from market_storage.models import Observation, Source, Window, asset_value
from market_storage.transfer import Remote
from tests.conftest import STAMP


def test_source_cadence_excludes_ohlcv() -> None:
    """Keep five-minute inputs and hourly lending on separate schedules."""
    fast, hourly = 300, 3600
    assert Source.OI.interval == Source.OPTIONS.interval == fast
    assert Source.GROWTH_HOURLY.interval == Source.LOAN_RATE.interval == hourly
    with pytest.raises(ValueError, match="candles"):
        _ = Source("candles")
    assert asset_value("ETH") == "ETH"
    with pytest.raises(ValueError, match="Asset"):
        _ = asset_value("SOL")


@pytest.mark.parametrize(
    "host", ["-oProxyCommand=bad", "a;touch /tmp/bad", "", "a\nb"]
)
def test_remote_rejects_ssh_option_injection(host: str) -> None:
    """Reject hostile SSH host operands before invoking any process."""
    with pytest.raises(ValueError, match="SSH"):
        _ = Remote(host=host, root="/srv/bot")


@pytest.mark.parametrize("root", ["relative", "~/bot", "/srv/bot\nbad"])
def test_remote_requires_absolute_root(root: str) -> None:
    """Make remote path resolution independent of SSH's working directory."""
    with pytest.raises(ValueError, match="absolute"):
        _ = Remote(host="vps", root=root)


@pytest.mark.parametrize("database", ["/etc/passwd", "../other.sqlite"])
def test_remote_database_stays_inside_deployment(database: str) -> None:
    """Keep the requested file within the explicitly selected deployment."""
    with pytest.raises(ValueError, match="relative"):
        _ = Remote(host="user@vps", root="/srv/bot", database=database)


def test_remote_quotes_paths_with_shell_metacharacters() -> None:
    """Treat a remote path as one literal operand, not executable shell text."""
    remote = Remote(host="user@vps", root="/srv/bot's $(touch bad)")
    command = remote.command()
    assert command[-2] == "user@vps"
    assert (
        shlex.split(command[-1].split("; exec ", 1)[1])[-1]
        == remote.root + "/server_bot/tunnel_inputs.sqlite"
    )


def test_window_rejects_invalid_time_bounds() -> None:
    """Reject naive timestamps and inverted windows before persistence."""
    with pytest.raises(ValueError, match="UTC offset"):
        _ = Window(asset="BTC", start=STAMP.replace(tzinfo=None), end=STAMP)
    with pytest.raises(ValueError, match="start"):
        _ = Window(asset="BTC", start=STAMP, end=STAMP - timedelta(seconds=1))


def test_observation_rejects_noncausal_metadata(
    observation: Observation,
) -> None:
    """Reject future requests and missing collection identities."""
    with pytest.raises(ValueError, match="causal"):
        _ = replace(observation, available=STAMP - timedelta(seconds=1))
    with pytest.raises(ValueError, match="identity"):
        _ = replace(observation, collection="")
