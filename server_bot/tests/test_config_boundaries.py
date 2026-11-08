# Copyright 2026 anchored-vwam-backtest contributors
"""Reject malformed persisted configuration before starting a trading bot."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import pytest

from live_paper_bot.config import PaperBotConfig as LiveConfig
from reference_bot.config import PaperBotConfig as ReferenceConfig
from shared.validation import json_object

if TYPE_CHECKING:
    from pathlib import Path

ConfigType = type[ReferenceConfig] | type[LiveConfig]


@pytest.fixture(params=[ReferenceConfig, LiveConfig])
def config_type(request: pytest.FixtureRequest) -> ConfigType:
    """Exercise both independently deployable configuration readers.

    Returns:
        The requested configuration class.

    Raises:
        TypeError: If the fixture contains an unsupported reader.

    """
    # Pytest parameters are untyped; validate the class identity below.
    value = cast("object", request.param)
    if value is ReferenceConfig:
        return ReferenceConfig
    if value is LiveConfig:
        return LiveConfig
    message = "Unsupported configuration reader."
    raise TypeError(message)


@pytest.fixture
def config_values() -> dict[str, object]:
    """Return a valid configuration with the legacy required fields.

    Returns:
        A fresh dictionary containing all required legacy settings.

    """
    return {
        "strategy_mode": "VWAP band mean reversion",
        "timeframe": "5m",
        "trend": True,
        "open_order_vwap_sigma": 1.2,
        "close_order_vwap_sigma": 0.4,
        "initial_capital": 1000,
        "stop_loss_pct": 2.5,
        "allow_immediate_reentry": False,
        "vwap_anchor_reset_weeks": 2,
    }


@pytest.mark.parametrize("payload", ["[]", "null", '"config"', "1", "{invalid"])
def test_load_rejects_invalid_json_roots(
    config_type: ConfigType, tmp_path: Path, payload: str
) -> None:
    """Report malformed documents with the configuration filename."""
    path = tmp_path / "config.json"
    _ = path.write_text(payload, encoding="utf-8")
    with pytest.raises(RuntimeError, match="Could not load paper bot config"):
        _ = config_type.load(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trend", "false"),
        ("initial_capital", True),
        ("initial_capital", float("nan")),
        ("initial_capital", float("inf")),
        ("vwap_anchor_reset_weeks", True),
        ("anchor_before_days", 1.5),
        ("ticker", ["BTC_USDT"]),
        ("open_sigma_1", "1"),
        ("unrecognized_setting", 1),
    ],
)
def test_load_rejects_invalid_field_types(
    *,
    config_type: ConfigType,
    tmp_path: Path,
    config_values: dict[str, object],
    field: str,
    value: object,
) -> None:
    """Reject invalid types, non-finite numbers, and unknown keys."""
    config_values[field] = value
    path = tmp_path / "config.json"
    _ = path.write_text(json.dumps(config_values), encoding="utf-8")
    with pytest.raises(RuntimeError, match="Could not load paper bot config"):
        _ = config_type.load(path)


def test_load_retains_legacy_defaults(
    config_type: ConfigType,
    tmp_path: Path,
    config_values: dict[str, object],
) -> None:
    """Continue reading old configuration files with exchange-managed fees."""
    config_values["fee_pct"] = 0.1
    path = tmp_path / "config.json"
    _ = path.write_text(json.dumps(config_values), encoding="utf-8")
    loaded = config_type.load(path)
    serialized = json_object(loaded.to_json())
    if loaded.initial_capital != config_values["initial_capital"]:
        pytest.fail(
            "Expected loaded.initial_capital =="
            " config_values['initial_capital']"
        )
    if loaded.ticker != "BTC_USDT":
        pytest.fail("Expected loaded.ticker == 'BTC_USDT'")
    if loaded.leverage != 1:
        pytest.fail("Expected loaded.leverage == 1")
    if not ("fee_pct" not in (serialized)):
        pytest.fail("Expected 'fee_pct' not in serialized")
    if not ("open_sigma_1" not in (serialized)):
        pytest.fail("Expected 'open_sigma_1' not in serialized")


@pytest.mark.parametrize(
    "field", ["open_sigma_1", "close_sigma_1", "open_sigma_2", "close_sigma_2"]
)
def test_load_rejects_incomplete_sigma_configuration(
    *,
    config_type: ConfigType,
    tmp_path: Path,
    config_values: dict[str, object],
    field: str,
) -> None:
    """Require both entry and exit settings for both sigma legs."""
    config_values[field] = 1
    path = tmp_path / "config.json"
    _ = path.write_text(json.dumps(config_values), encoding="utf-8")
    with pytest.raises(ValueError, match="All four real-order sigma"):
        _ = config_type.load(path)


def test_config_serialization_survives_round_trip(
    config_type: ConfigType, tmp_path: Path
) -> None:
    """Keep reference and executor fingerprints identical after loading."""
    original = config_type(
        strategy_mode="VWAP band mean reversion",
        timeframe="5m",
        trend=True,
        open_order_vwap_sigma=1,
        close_order_vwap_sigma=2,
        initial_capital=1000,
        stop_loss_pct=1,
        allow_immediate_reentry=False,
        vwap_anchor_reset_weeks=2,
        minimum_order_size=1,
        open_sigma_1=1,
        close_sigma_1=2,
        open_sigma_2=-1,
        close_sigma_2=-2,
    )
    path = tmp_path / "config.json"
    original.save(path)
    if config_type.load(path).to_json() != original.to_json():
        pytest.fail(
            "Expected config_type.load(path).to_json() == original.to_json()"
        )
