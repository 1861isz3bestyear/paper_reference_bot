"""Atomic, versioned handoff from reference accounting to the demo executor."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def config_fingerprint(config) -> str:
    return hashlib.sha256(config.to_json().encode()).hexdigest()


def publish_target(path: Path, config, state, position) -> None:
    payload = {
        "version": 1,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "reference_run": state.launched_at,
        "config_fingerprint": config_fingerprint(config),
        "symbol": position.symbol,
        "timeframe": config.timeframe,
        "last_processed_candle": state.last_processed_candle,
        "side": position.side,
        "quantity": str(position.quantity),
        "entry_price": position.entry_price,
        "position_id": position.updated_at if position.side else None,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
