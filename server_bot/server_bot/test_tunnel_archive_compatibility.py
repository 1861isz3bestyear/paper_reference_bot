# Copyright 2026 anchored-vwam-backtest contributors
"""Read archived exchange responses using the actual calculator parsers."""

from __future__ import annotations

import asyncio
import json
import sqlite3
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
    growth = {
        "code": "00000",
        "data": [{"ts": str(int(now.timestamp() * 1000)), "growthRate": "0.2"}],
    }
    payloads.update({
        "bitget_growth_24h": growth,
        "bitget_growth_30d": growth,
        "bitget_loan_rate": {
            "code": "00000",
            "requestTime": int(now.timestamp() * 1000),
            "data": {"dailyInterest": "0.001", "annualInterest": "0.365"},
        },
    })
    session = requests.Session()
    responses = iter(sources(asset))

    def get(*_args: object, **_kwargs: object) -> requests.Response:
        """Get.

        Returns:
            The get result.

        """
        source = next(responses)
        response = requests.Response()
        response.status_code = 200
        response.raw = BytesIO(
            json.dumps(
                payloads.get(
                    source.name, {"result": {}, "code": "00000", "data": []}
                )
            ).encode()
        )
        return response

    monkeypatch.setattr(session, "get", get)
    path = tmp_path / "archive.sqlite"
    assert collect(path, session, asset=asset) == 0
    # Separate process prevents imports from the server's shared package from
    # masking incompatibilities with the calculator's installed source tree.
    script = """
import sqlite3
from contextlib import closing
from io import BytesIO
from pathlib import Path
import sys
from datetime import datetime
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, str(Path(sys.argv[1]).parent / "margin_loan_oscillator"))
from gui_calculator.options_data import normalize_chain
from gui_calculator.options_depth import parse_depth
from oscillator.bitget import parse_growth, parse_rate
from shared.validation import json_object
with closing(sqlite3.connect(sys.argv[2])) as db:
    rows = {r[0]: r[1:] for r in db.execute(
        "SELECT source, payload, available_at FROM tunnel_inputs "
        "WHERE error = ''"
    )}
chain = normalize_chain(json_object(rows['instruments'][0])['result'],
                        json_object(rows['options'][0])['result'],
                        asset=sys.argv[3])
assert parse_growth(rows['bitget_growth_24h'][0])[0].growth == 0.2
assert parse_growth(rows['bitget_growth_30d'][0])[0].growth == 0.2
assert parse_rate(rows['bitget_loan_rate'][0]).daily == 0.001
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
    asyncio.run(check_calculator(script, calculator, path, asset=asset))
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT COUNT(*) FROM tunnel_inputs").fetchone() == (
            9,
        )


async def check_calculator(
    script: str, calculator: Path, archive: Path, *, asset: str
) -> None:
    """Run the calculator parser in an isolated interpreter."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(calculator),
        str(archive),
        asset,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, error = await process.communicate()
    assert await process.wait() == 0, (output.decode(), error.decode())
