# Copyright 2026 anchored-vwam-backtest contributors
"""Exercise shared configuration persistence and invalid domain settings."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, time, timedelta
from typing import TYPE_CHECKING

import pytest

from shared.daily_gex import (
    DailySettings,
    FlowEvidence,
    FlowMode,
    FlowSettings,
    FreshConfirmation,
    WallSettings,
    normalized_slope,
)
from shared.gex_config import GexPaperConfig, GexRisk
from shared.paper_config import PaperBotConfig
from shared.tunnel import TunnelSettings, TunnelState, oscillator_vote
from shared.tunnel_config import TUNNEL_MODE, parse_tunnel, tunnel_values
from shared.validation import json_object

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def shared_config() -> PaperBotConfig:
    """Return a valid nonleveraged mean-reversion configuration.

    Returns:
        The configured test value.

    """
    return PaperBotConfig(
        strategy_mode="VWAP band mean reversion",
        timeframe="5m",
        trend=True,
        open_order_vwap_sigma=1,
        close_order_vwap_sigma=2,
        initial_capital=1000,
        stop_loss_pct=2,
        allow_immediate_reentry=True,
        vwap_anchor_reset_weeks=2,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("strategy_mode", "unknown"),
        ("timeframe", "2m"),
        ("open_order_vwap_sigma", 0),
        ("close_order_vwap_sigma", 4),
        ("stop_loss_basis", "unknown"),
        ("hedge_entry_timeout_bars", True),
        ("hedge_entry_timeout_bars", 0),
        ("initial_capital", 0),
        ("minimum_order_size", 0),
        ("stop_loss_pct", -1),
        ("take_profit_pct", -1),
        ("take_profit_pct", math.nan),
        ("vwap_anchor_reset_weeks", 0),
        ("anchor_before_strategy_start", 1),
        ("anchor_before_days", True),
        ("anchor_before_days", -1),
        ("anchor_before_strategy_start", True),
        ("data_source", "unknown"),
        ("ticker", "UNKNOWN"),
        ("ticker", "SHIB_USDT"),
        ("reverse_ticker", 1),
        ("open_position_side", "unknown"),
    ],
)
def test_shared_config_rejects_invalid_fields(
    shared_config: PaperBotConfig,
    field: str,
    value: object,
) -> None:
    """Reject malformed settings before exporting or saving them."""
    with pytest.raises(
        ValueError,
        match=r"must|Unsupported|cannot|negative|currently|Stop|Initial|Minimum|Entry|Take|Reverse|Open",
    ):
        replace(shared_config, **{field: value}).validate()


def test_shared_config_exports_and_exclusive_presets(
    shared_config: PaperBotConfig,
    tmp_path: Path,
) -> None:
    """Round-trip settings and refuse to overwrite named server exports."""
    path = tmp_path / "local.json"
    shared_config.save(path)
    if PaperBotConfig.load(path) != shared_config:
        pytest.fail("Expected PaperBotConfig.load(path) == shared_config")
    saved = shared_config.save_server(" preset.json ", tmp_path / "presets")
    values = json_object(saved.read_text())
    if values["open_sigma_1"] != 1:
        pytest.fail("Expected values['open_sigma_1'] == 1")
    if values["open_sigma_2"] != -1:
        pytest.fail("Expected values['open_sigma_2'] == -1")
    with pytest.raises(FileExistsError):
        _ = shared_config.save_server("preset", tmp_path / "presets")
    with pytest.raises(ValueError, match=r"starting with"):
        _ = shared_config.save_server("../escape", tmp_path)
    zero = replace(shared_config, close_order_vwap_sigma=0)
    if not ("open_sigma_1" not in json_object(zero.to_server_json())):
        pytest.fail(
            "Expected 'open_sigma_1' not in json_object(zero.to_server_json())"
        )
    with pytest.raises(RuntimeError, match=r"not found"):
        _ = PaperBotConfig.load(tmp_path / "missing.json")
    _ = path.write_text("[]")
    with pytest.raises(RuntimeError, match=r"Could not load"):
        _ = PaperBotConfig.load(path)
    _ = path.write_text('{"unknown": 1}')
    with pytest.raises(RuntimeError, match=r"Unknown"):
        _ = PaperBotConfig.load(path)


def test_shared_config_leverage_and_hedge_constraints(
    shared_config: PaperBotConfig,
    tmp_path: Path,
) -> None:
    """Keep server-only leverage and two-sided hedge settings explicit."""
    leveraged = replace(shared_config, leverage=2)
    leveraged.validate()
    with pytest.raises(ValueError, match=r"Lima"):
        _ = leveraged.to_json()
    path = tmp_path / "leveraged.json"
    _ = path.write_text(leveraged.to_server_json())
    # Local exports deliberately do not accept the server signed-leg schema.
    values = json_object(path.read_text())
    for key in (
        "open_sigma_1",
        "open_sigma_2",
        "close_sigma_1",
        "close_sigma_2",
    ):
        _ = values.pop(key)
    _ = path.write_text(json.dumps(values))
    with pytest.raises(ValueError, match=r"Lima"):
        _ = PaperBotConfig.load(path)
    with pytest.raises(ValueError, match=r"Leverage requires"):
        replace(leveraged, reverse_ticker=True).validate()
    hedge = replace(
        shared_config, strategy_mode="VWAP zero hedge", open_order_vwap_sigma=0
    )
    hedge.validate()
    with pytest.raises(ValueError, match=r"requires"):
        replace(hedge, open_position_side="Long").validate()
    with pytest.raises(ValueError, match=r"requires"):
        replace(hedge, reverse_ticker=True).validate()
    with pytest.raises(ValueError, match=r"Negative"):
        replace(
            shared_config,
            strategy_mode="AVWAP crossover",
            close_order_vwap_sigma=-1,
        ).validate()


def test_tunnel_config_round_trip(
    shared_config: PaperBotConfig, tmp_path: Path
) -> None:
    """Preserve explicit tunnel durations and reject incompatible execution."""
    settings = TunnelSettings(state_override=TunnelState.BOTH)
    if parse_tunnel(tunnel_values(settings)) != settings:
        pytest.fail(
            "Expected parse_tunnel(tunnel_values(settings)) == settings"
        )
    if parse_tunnel({}) != TunnelSettings():
        pytest.fail("Expected parse_tunnel({}) == TunnelSettings()")
    with pytest.raises(ValueError, match=r"Unknown"):
        _ = parse_tunnel({"future": True})
    config = replace(
        shared_config,
        strategy_mode=TUNNEL_MODE,
        take_profit_pct=3,
        tunnel=settings,
    )
    path = tmp_path / "tunnel.json"
    config.save(path)
    if PaperBotConfig.load(path) != config:
        pytest.fail("Expected PaperBotConfig.load(path) == config")
    with pytest.raises(ValueError, match=r"not Lima"):
        _ = config.to_server_json()
    with pytest.raises(ValueError, match=r"BTC_USDT"):
        replace(config, reverse_ticker=True).validate()
    with pytest.raises(ValueError, match=r"positive"):
        replace(config, take_profit_pct=0).validate()


@pytest.mark.parametrize(
    "settings",
    [
        GexPaperConfig(),
        GexPaperConfig(
            daily=DailySettings(
                fresh_confirmation=FreshConfirmation(),
                walls=WallSettings(stop_offset=None),
            )
        ),
    ],
)
def test_daily_config_round_trip(
    settings: GexPaperConfig, tmp_path: Path
) -> None:
    """Preserve optional fresh confirmation and wall stops in daily exports."""
    path = tmp_path / "gex.json"
    _ = path.write_text(settings.to_json())
    if GexPaperConfig.load(path) != settings:
        pytest.fail("Expected GexPaperConfig.load(path) == settings")
    _ = path.write_text('{"strategy": "unknown"}')
    with pytest.raises(ValueError, match=r"daily_gex"):
        _ = GexPaperConfig.load(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capital", 0),
        ("fee_pct", 100),
        ("minimum_order", 0),
        ("stop_loss_pct", math.nan),
        ("side", "unknown"),
    ],
)
def test_daily_risk_rejects_invalid_values(field: str, value: object) -> None:
    """Reject invalid account amounts, percentages and side selections."""
    with pytest.raises(ValueError, match=r"Invalid GEX|Open position"):
        _ = replace(GexRisk(), **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("poll_seconds", 0),
        ("poll_seconds", math.inf),
        ("maximum_quote_age", timedelta(seconds=31)),
    ],
)
def test_daily_poll_rejects_invalid_values(field: str, value: object) -> None:
    """Bound polling and quote staleness at the configuration boundary."""
    with pytest.raises(ValueError, match=r"polling"):
        _ = replace(GexPaperConfig(), **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("oi_window", timedelta(0)),
        ("oi_delay", timedelta(seconds=-1)),
        ("threshold", 1.1),
        ("volume_tolerance", math.nan),
    ],
)
def test_flow_settings_validation(field: str, value: object) -> None:
    """Require causal windows and finite confirmation thresholds."""
    with pytest.raises(ValueError, match=r"Flow windows"):
        _ = replace(FlowSettings(), **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [("proximity", -1), ("momentum_offset", 1), ("neutral_gex", -1)],
)
def test_wall_settings_validation(field: str, value: object) -> None:
    """Reject invalid wall placement and neutral exposure thresholds."""
    with pytest.raises(ValueError, match=r"Invalid wall"):
        _ = replace(WallSettings(), **{field: value})


def test_daily_time_validation() -> None:
    """Reject noncausal freshness limits and non-UTC schedule clocks."""
    with pytest.raises(ValueError, match=r"Fresh confirmation|UTC clock"):
        _ = FreshConfirmation(maximum_age=timedelta(0))
    with pytest.raises(ValueError, match=r"Fresh confirmation|UTC clock"):
        _ = FreshConfirmation(wall_drift=1)
    with pytest.raises(ValueError, match=r"Fresh confirmation|UTC clock"):
        _ = DailySettings(refresh=time(tzinfo=UTC))
    with pytest.raises(ValueError, match=r"Fresh confirmation|UTC clock"):
        _ = DailySettings(snapshot_age=timedelta(0))


@pytest.mark.parametrize("mode", list(FlowMode))
def test_daily_independent_flow_filters(mode: FlowMode) -> None:
    """Require direction and the correct activity sign for each GEX regime."""
    settings = FlowSettings(mode=mode)
    evidence = FlowEvidence(
        trades=0.5, book=0.5, oi_slope=-0.1, volume_change=-0.1
    )
    if not (evidence.confirms(1, gex=1, settings=settings)):
        pytest.fail("Expected evidence.confirms(1, gex=1, settings=settings)")
    if evidence.confirms(-1, gex=1, settings=settings):
        pytest.fail(
            "Expected not evidence.confirms(-1, gex=1, settings=settings)"
        )
    if evidence.confirms(1, gex=-1, settings=settings):
        pytest.fail(
            "Expected not evidence.confirms(1, gex=-1, settings=settings)"
        )
    if not (
        evidence.confirms(
            1,
            gex=-1,
            settings=replace(settings, use_oi=False, use_volume=False),
        )
    ):
        pytest.fail(
            "Expected evidence.confirms(1, gex=-1, settings=replace(settings,"
            " use_oi=False, use_volume=False))"
        )
    if FlowEvidence().confirms(1, gex=1, settings=settings):
        pytest.fail(
            "Expected not FlowEvidence().confirms(1, gex=1, settings=settings)"
        )


def test_relative_oi_regression() -> None:
    """Use actual sample times and reject insufficient or degenerate data."""
    if normalized_slope(((0, 100), (1, 101), (2, 102))) != pytest.approx(
        1 / 101
    ):
        pytest.fail(
            "Expected normalized_slope(((0, 100), (1, 101), (2, 102))) =="
            " pytest.approx(1 / 101)"
        )
    for points in (
        (),
        ((0, 1),),
        ((0, 1), (0, 2), (0, 3)),
        ((0, 0), (1, 0), (2, 0)),
    ):
        if not (math.isnan(normalized_slope(points))):
            pytest.fail("Expected math.isnan(normalized_slope(points))")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tight_width", 0),
        ("proximity", math.nan),
        ("votes", 2),
        ("long_threshold", 30),
        ("maximum_age", timedelta(0)),
    ],
)
def test_tunnel_settings_validation(field: str, value: object) -> None:
    """Reject impossible votes, distances, thresholds and freshness limits."""
    with pytest.raises(
        ValueError,
        match=r"Tunnel distances|confirmations|thresholds|maximum age",
    ):
        _ = replace(TunnelSettings(), **{field: value})


@pytest.mark.parametrize(
    ("rate", "vote"), [(math.nan, 0), (60, 1), (40, -1), (50, 0)]
)
def test_oscillator_thresholds(rate: float, vote: int) -> None:
    """Honor inclusive directional boundaries and missing observations."""
    if oscillator_vote(rate, TunnelSettings()) != vote:
        pytest.fail("Expected oscillator_vote(rate, TunnelSettings()) == vote")
