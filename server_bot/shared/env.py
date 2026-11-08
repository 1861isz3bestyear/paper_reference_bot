# Copyright 2026 anchored-vwam-backtest contributors
"""Provide env behavior."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def load_env(path: Path) -> dict[str, str]:
    """Handle load env.

    Returns:
        The resulting value.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    if not path.is_file():
        msg = f"Environment file not found: {path}"
        raise ValueError(msg)
    values: dict[str, str] = {}
    for number, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            msg = f"Invalid .env entry on line {number}"
            raise ValueError(msg)
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def setting(
    values: dict[str, str], name: str, default: str | None = None
) -> str:
    """Handle setting.

    Returns:
        The resulting value.

    Raises:
        ValueError: If the operation cannot be completed.

    """
    value = os.getenv(name) or values.get(name) or default
    if value is None or not value.strip():
        msg = f"Missing required setting {name}"
        raise ValueError(msg)
    return value.strip()
