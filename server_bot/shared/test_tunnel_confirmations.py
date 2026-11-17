# Copyright 2026 anchored-vwam-backtest contributors
"""Validate exported confirmation configurations in both deployable projects."""

import pytest

from shared.option_levels import OptionLevelRange
from shared.tunnel import OiConfirmationMode, TunnelSettings
from shared.tunnel_config import parse_tunnel, tunnel_values


def test_oscillator_only_requires_a_positive_quorum() -> None:
    """Require positive quorum when oscillator-only sources are enabled."""
    with pytest.raises(ValueError, match="at least one confirmation"):
        _ = TunnelSettings(use_gex=False, votes=0)
    with pytest.raises(ValueError, match="at least one confirmation"):
        _ = parse_tunnel({"use_gex": False, "votes": 0})
    disabled = TunnelSettings(use_gex=False, use_book=False, votes=0)
    assert parse_tunnel(tunnel_values(disabled)) == disabled


@pytest.mark.parametrize("mode", list(OiConfirmationMode))
def test_confirmation_switches_and_oi_mode_survive_export(
    mode: OiConfirmationMode,
) -> None:
    """Export explicit raw OI convergence without restoring GEX filtering."""
    settings = TunnelSettings(
        use_gex=False,
        use_book=False,
        use_margin=True,
        use_oi=True,
        oi_mode=mode,
        votes=2,
    )
    assert parse_tunnel(tunnel_values(settings)) == settings


@pytest.mark.parametrize("enabled", [False, True])
def test_level_range_survives_export(*, enabled: bool) -> None:
    """Keep the chosen distance when disabling and exporting the range."""
    settings = TunnelSettings(
        level_range=OptionLevelRange(enabled=enabled, distance=0.35)
    )
    assert parse_tunnel(tunnel_values(settings)) == settings
    assert parse_tunnel({}).level_range == OptionLevelRange()


@pytest.mark.parametrize(
    "value",
    [
        {"enabled": True},
        {"enabled": True, "distance": 0.2, "extra": 1},
        {"enabled": "true", "distance": 0.2},
        {"enabled": True, "distance": "0.2"},
        {"enabled": True, "distance": 0},
        {"enabled": False, "distance": 1},
        {"enabled": True, "distance": float("nan")},
    ],
)
def test_level_range_rejects_invalid_json(value: object) -> None:
    """Reject malformed external range settings and invalid distances."""
    with pytest.raises((TypeError, ValueError), match=r"."):
        _ = parse_tunnel({"level_range": value})
