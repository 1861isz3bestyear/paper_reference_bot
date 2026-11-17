# Copyright 2026 anchored-vwam-backtest contributors
"""Validate the tunnel strategy's JSON boundary."""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta

from shared.option_levels import OptionLevelRange
from shared.tunnel import OiConfirmationMode, TunnelSettings, TunnelState
from shared.validation import boolean, integer, number, object_mapping, string

TUNNEL_MODE = "Snapshot tunnel"


def tunnel_values(settings: TunnelSettings) -> dict[str, object]:
    """Serialize distances, confirmation switches and tunnel state.

    Returns:
        JSON-compatible strategy parameters.

    """
    values: dict[str, object] = asdict(settings)
    values["maximum_age"] = settings.maximum_age.total_seconds()
    return values


def parse_tunnel(value: object) -> TunnelSettings:
    """Reject unknown fields and validate every external setting.

    Returns:
        Validated immutable tunnel parameters.

    Raises:
        ValueError: If an unknown setting or invalid state is supplied.

    """
    values = object_mapping(value)
    defaults = tunnel_values(TunnelSettings())
    if values.keys() - defaults.keys():
        msg = "Unknown snapshot tunnel configuration fields."
        raise ValueError(msg)
    if values.get("use_margin") is True and "margin_source" not in values:
        msg = (
            "Legacy margin configuration must be re-exported for Bitget growth."
        )
        raise ValueError(msg)
    defaults.update(values)
    if defaults["margin_source"] != "bitget_growth":
        msg = "Only Bitget growth is supported for the margin oscillator."
        raise ValueError(msg)
    state = defaults["state_override"]
    level_range = object_mapping(defaults["level_range"])
    if level_range.keys() != {"enabled", "distance"}:
        msg = "Option level range requires exactly enabled and distance fields."
        raise ValueError(msg)
    return TunnelSettings(
        level_range=OptionLevelRange(
            enabled=boolean(level_range["enabled"]),
            distance=number(level_range["distance"]),
        ),
        tight_width=number(defaults["tight_width"]),
        proximity=number(defaults["proximity"]),
        insurance_offset=number(defaults["insurance_offset"]),
        negative_long_tp_offset=number(defaults["negative_long_tp_offset"]),
        cap_negative_long_tp=boolean(defaults["cap_negative_long_tp"]),
        state_override=None if state is None else TunnelState(integer(state)),
        use_gex=boolean(defaults["use_gex"]),
        use_book=boolean(defaults["use_book"]),
        use_margin=boolean(defaults["use_margin"]),
        use_oi=boolean(defaults["use_oi"]),
        oi_mode=OiConfirmationMode(string(defaults["oi_mode"])),
        oi_momentum_minimum=number(defaults["oi_momentum_minimum"]),
        votes=integer(defaults["votes"]),
        long_threshold=number(defaults["long_threshold"]),
        short_threshold=number(defaults["short_threshold"]),
        maximum_age=timedelta(seconds=number(defaults["maximum_age"])),
    )
