# Copyright 2026 anchored-vwam-backtest contributors
"""Read archived exchange responses using the actual calculator parsers."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
import requests

from server_bot.tunnel_archive import collect, sources


@pytest.mark.contract
@pytest.mark.parametrize("asset", ["BTC", "ETH"])
def test_archive_matches_calculator_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asset: str
) -> None:
    """Preserve chain metadata, IV units, full book levels and receipt times."""
    calculator = Path(__file__).resolve().parents[4] / "backtest"
    if not calculator.is_dir():
        pytest.skip("Compatibility requires the sibling backtest project")
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    now = datetime.now(UTC)
    instrument = f"{asset}-TEST-C"
    payloads: dict[str, object] = {
        "instruments": {
            "result": [{
                "instrument_name": instrument,
                "kind": "option",
                "is_active": True,
                "base_currency": asset,
                "option_type": "call",
                "contract_size": 1,
                "expiration_timestamp": int(
                    (now + timedelta(days=10)).timestamp() * 1000
                ),
                "strike": 100000,
            }]
        },
        "options": {
            "result": [{
                "instrument_name": instrument,
                "open_interest": 20,
                "mark_iv": 50,
            }],
            "usOut": int(now.timestamp() * 1000000),
        },
        "spot": {"result": {"index_price": 100000}},
        "depth": {
            "retCode": 0,
            "result": {
                "s": f"{asset}USDT",
                "ts": int(now.timestamp() * 1000),
                "b": [["99999", "2"], ["99998", "3"]],
                "a": [["100001", "4"], ["100002", "5"]],
            },
        },
    }
    session = requests.Session()
    responses = iter(sources(asset))

    def get(*_args: object, **_kwargs: object) -> requests.Response:
        """Get."""
        source = next(responses)
        response = requests.Response()
        response.status_code = 200
        response.raw = BytesIO(
            json.dumps(payloads.get(source.name, {"result": {}})).encode()
        )
        return response

    monkeypatch.setattr(session, "get", get)
    path = tmp_path / "archive.sqlite"
    if collect(path, session, asset=asset) != 1:
        pytest.fail("Expected collect(path, session, asset=asset) == 1")
    # Separate process prevents imports from the server's shared package from
    # masking incompatibilities with the calculator's installed source tree.
    script = """
import sqlite3
from contextlib import closing
from io import BytesIO
import sys
from datetime import datetime
sys.path.insert(0, sys.argv[1])
from gui_calculator.options_data import normalize_chain
from gui_calculator.options_depth import parse_depth
from shared.validation import json_object
with closing(sqlite3.connect(sys.argv[2])) as db:
    rows = {r[0]: r[1:] for r in db.execute(
        "SELECT source, payload, available_at FROM tunnel_inputs WHERE error = ''"
    )}
chain = normalize_chain(json_object(rows['instruments'][0])['result'],
                        json_object(rows['options'][0])['result'], asset=sys.argv[3])
assert len(chain) == 1
assert chain[0].iv == 0.5
assert chain[0].open_interest == 20
assert chain[0].multiplier == 1
book = parse_depth(rows['depth'][0], asset=sys.argv[3],
                   received=datetime.fromisoformat(rows['depth'][1]))
assert len(book.bids) == len(book.asks) == 2
assert book.bids[0].price == 99999
assert book.asks[1].quantity == 5
assert book.available == datetime.fromisoformat(rows['depth'][1])
assert json_object(rows['spot'][0])['result']['index_price'] == 100000
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(calculator), str(path), asset],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(result.stderr)
    with closing(sqlite3.connect(path)) as db:
        if db.execute("SELECT COUNT(*) FROM tunnel_inputs").fetchone() != (9,):
            pytest.fail(
                "Expected db.execute('SELECT COUNT(*) FROM"
                " tunnel_inputs').fetchone() == (9,)"
            )
