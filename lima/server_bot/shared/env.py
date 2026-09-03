from __future__ import annotations

import os
from pathlib import Path


def load_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValueError(f"Environment file not found: {path}")
    values = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Invalid .env entry on line {number}")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def setting(values: dict[str, str], name: str, default: str | None = None) -> str:
    value = os.getenv(name) or values.get(name) or default
    if value is None or not value.strip():
        raise ValueError(f"Missing required setting {name}")
    return value.strip()
