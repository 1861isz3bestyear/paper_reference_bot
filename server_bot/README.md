# Server Bot

Runs two anchored VWAP paper bots from one repository and the exact `paper_bot_config.json` exported by the `backtest` calculator:

- `reference_bot` is sourced from `anchored_vwam_backtest/paper_trading_bot`.
- `live_paper_bot` is sourced from `anchored_vwam_paper_bot/live_paper_trading_bot`.

Both bots receive identical strategy and market settings. Their ledgers, cursors, candle caches, logs, PIDs, and instance locks are separate, so they can run concurrently.

## Install

Requires Python 3.14 or newer, matching the calculator shared engine.

For selectable Debian user-systemd installation and automated diagnostic archives, use the
centralized [`../install`](../install/README.md) utility.

```bash
uv sync --locked
uv run pytest
```

The default test run enforces at least **92% combined line/branch coverage**
across all production modules in `reference_bot`, `live_paper_bot`,
`bybit_demo_bot`, `bybit_bot`, `server_bot`, and `shared`, including modules
that are not imported by the tests. Test files are omitted from measurement.
Coverage failures return a nonzero exit status even when every test passes.
Use `uv run pytest --cov-report=html` for a browsable missing-coverage report.

Backtest is the source of truth for `shared/`. The default suite checks that
paper's shared filenames and contents match backtest byte for byte. Shared
behavior tests also cover configuration persistence, causal open interest,
VWAP reset boundaries, tunnel execution and nested hedge state recovery.

Copy and edit the shared configuration if needed:

```bash
cp paper_bot_config.example.json paper_bot_config.json
nvim paper_bot_config.json
```

Mainnet Bybit credentials for the read-only account fee-rate request are loaded from
`bybitapi.env` by both paper bots:

```bash
cp bybitapi.env.example bybitapi.env
chmod 600 bybitapi.env
```

## Combined commands

```bash
uv run python -m server_bot.cli start
uv run python -m server_bot.cli status
uv run python -m server_bot.cli stats
uv run python -m server_bot.cli stop
uv run python -m server_bot.cli reset
uv run python -m server_bot.cli health-check
```

Use `start --resume` to retain both launch anchors and processing cursors.

## Independent foreground processes

For systemd, supervise each process directly rather than using the background `start` command:

```bash
uv run python -m reference_bot.cli run --resume --config ./paper_bot_config.json
uv run python -m live_paper_bot.cli run --resume --config ./paper_bot_config.json
```

Create two systemd services with the same working directory and config path, one for each command above.

## Runtime isolation

The reference bot writes `reference_*` files. The live-paper bot writes `live_paper_*` files. `reset` deletes both bots' runtime data but retains the shared configuration and logs.

Neither bot submits real exchange orders. Bybit credentials are used only for the read-only fee-rate endpoint. MEXC uses public market and fee data.

## Bybit demo: Snapshot tunnel only

The demo command accepts **Save paper bot config** / **Download paper bot config**
from GUI Calculator. It rejects AVWAP, VWAP-band and hedge strategies. It runs its
own archive-backed decisions; no reference bot or live-paper process is required.
The canonical calculation package is `../snapshot_tunnel/`, shared directly with
backtest. Deploy that sibling directory together with `../storage/` and this
project before synchronizing dependencies.

From the deployment root on the VPS:

```bash
uv sync --locked --project server_bot
cd server_bot
uv run --locked python -m bybit_demo_bot.cli run \
  --config configs/my_gex_setup.json \
  --env bybitdemoapi.env \
  --archive tunnel_inputs.sqlite \
  --state bybit_demo_tunnel.sqlite \
  --resume
```

The environment file contains `BYBIT_API_KEY` and `BYBIT_API_SECRET` from a
**Bybit demo account**. The command uses `https://api-demo.bybit.com`; it does not
select a mainnet endpoint. Keep the archive collector running. An existing
`server_bot.cli run-bybit-demo --config ... --resume` service also selects this
executor, using the default archive and journal paths above. Do not start
`server_bot.cli start` for this strategy: that command starts the older reference
and live-paper applications.

Positive GEX uses states 1/2/3 and eligible option-level proximity. Negative GEX
uses oscillator direction without positive-state wall restrictions. Export all
three oscillators enabled and required votes set to 2 for a two-vote quorum.
Enabled OI must additionally agree: book and margin cannot override absent or
opposing OI. Missing, stale and future evidence never votes. Warmup can prevent
entries even when the service is running correctly. Plans and option-level range
settings use the same implementation as backtest.

Execution is BTCUSDT, one-way mode, 1x. Saved initial capital bounds entry notional,
further capped by available demo funds with a reserve. Exchange minimum size and
price grids can reject very small saved allocations. A market entry carries
exchange-side SL/TP; after a confirmed fill, percentage protection is recalculated
from the actual entry while retaining any frozen negative-long gamma-flip cap.
Live fills, fees and timing can differ from a historical simulation. The export
contains the main BTC strategy; separate price-only child simulations are not
orders in this demo executor.

The dedicated SQLite journal records each candle decision and order intent
before submission. Existing journals require `--resume`; changing the configuration
requires a separate journal and no outstanding exposure from the previous run.
Use a dedicated demo BTC position: foreign positions and open orders block entry.
An ambiguous order response is queried by its deterministic order link and is
never automatically resubmitted. If the exchange cannot establish its outcome,
execution remains paused; inspect exchange orders before intervening in the
journal. Stop the process normally to leave exchange protection in place.

## Real-money Bybit executor

`bybit_bot` uses the shared strategy and legacy execution policy, 90%-of-available-USDT sizing, and mandatory
exchange-side SL/TP protection. It remains an independent executor and sends orders to Bybit mainnet at
`https://api.bybit.com`. Its credentials, state, and instance lock are isolated from Demo Trading.

Create a dedicated trading key with withdrawals disabled:

```bash
cp bybitrealapi.env.example bybitrealapi.env
chmod 600 bybitrealapi.env
nvim bybitrealapi.env
```

Run it manually only with explicit live-funds confirmation:

```bash
uv run python -m bybit_bot.cli run --confirm-live --resume \
  --config ./paper_bot_config.json --env ./bybitrealapi.env
```

The combined CLI equivalent is:

```bash
uv run python -m server_bot.cli run-bybit-mainnet --confirm-live --resume \
  --config ./paper_bot_config.json --env ./bybitrealapi.env
```

Install only its systemd service explicitly; it is never part of the installer's default safe set:

```bash
cd ../install
uv run --project ../server_bot python bot_services.py install --entity bybit
```

The service is named `bybit-mainnet.service`. Treat this executor as real-money software: validate
the configuration and API permissions, start with a small funded balance, and monitor its first
entry and protection orders directly at Bybit.

## Health command for external supervision

`health-check` is read-only and contains no systemd or restart behavior. It compares both state files and returns:

- exit `0`: reference is fewer than 10 candles behind live-paper;
- exit `1`: live-paper is current but reference is at least 10 candles behind;
- exit `2`: health is indeterminate because live-paper itself is stale or state is unavailable.

Thresholds can be changed with `--stale-candles` and `--paper-grace-candles`. A systemd health service or timer can later use these exit codes to decide whether to restart the reference service.

### Exchange-side exits and re-entry

The mainnet executor submits at most one entry per uninterrupted
strategy direction. If an exchange-side stop, take-profit, or manual close leaves
the account flat, the executor waits until the strategy becomes flat or changes
direction before allowing another entry. This guard persists with `--resume`.
Existing state files without the guard conservatively consume the current signal
on upgrade, waiting for a signal change before entering. Order submission errors
also consume the signal because the exchange may have accepted the request.

## Tests

The server suite exercises archive collection, legacy reference/mainnet components,
and Snapshot tunnel demo execution. Tunnel tests cover real archive receipts,
order intent before submission, lost acknowledgements, restart recovery,
protection, sizing and rejection of other strategies. The calculator suite also
covers the canonical strategy package and the production UI in Chromium.

The calculator and server `shared/` compatibility directories remain identical.
Their parity tests compare source bytes; the strategy implementation is owned by
`../snapshot_tunnel/` and imported by both applications.

## VWAP zero hedge

The calculator and bots support simultaneous Long/Short limit entries at 0σ,
symmetric decimal sigma targets, and Position/Deposit stop budgets. See the
[strategy and configuration guide](../../../../ZERO_HEDGE_STRATEGY.md) for fill timing, allocation,
expiry, hedge position mode, and restart behavior.

## Backtest configurations

The calculator's named server export writes to `configs/` in this project.
Exports preserve strategy, timeframe, ticker, sides, anchors, capital, stop loss,
take profit, reentry, leverage and slippage settings. Existing names are never
replaced. Snapshot tunnel is supported only by the local backtest paper bot.

List exported configs and select one explicitly for both paper processes:

```bash
uv run python -m server_bot.cli configs
uv run python -m server_bot.cli start --config ./configs/my_strategy.json
```

Pass the same `--config` path to `status`, `stats`, `health-check`, or either
independent foreground process. Copy `configs/` along with the project when
installing on another machine. Exporting or listing a config does not change a
running bot; stop the paper processes before starting with a different config.

The demo executor prefers `bybitdemoapi.env`, with `bybitapidemo.env` as a legacy fallback.
The demo loader accepts `BYBIT_API_KEY` / `BYBIT_API_SECRET` and `API_KEY` / `SECRET_API_KEY`.

### Scheduled market input archive

Run the collector from this directory:

```bash
uv run --locked python -m server_bot.tunnel_archive
uv run --locked python -m server_bot.tunnel_archive --once
```

For BTC (or `--asset ETH`), the collector uses independent UTC schedules:

| Inputs | Collection cadence |
| --- | --- |
| Deribit instrument definitions, option IV/OI summaries and index price | 5 minutes |
| Bybit top-50 order book and current ticker price | 5 minutes |
| Bybit latest 12 historical OI observations (`intervalTime=5min`, `limit=12`) | 5 minutes |
| Bitget hourly/daily loan-growth windows and current VIP0 interest quote | 1 hour |

Each OI poll overlaps the previous hour to recover samples skipped by API
publication timing or short interruptions. Raw responses retain the actual
receipt time; historical samples must not be treated as known before receipt.
Interruptions longer than the overlap can still leave gaps.

No candle OHLCV, exchange credentials or trading orders are involved. All nine
sources are public. Bitget requests are spaced one second apart. `--poll-seconds`
controls the scheduler wake-up (default 300; supported range 1–300), not the
source intervals. The 30d Bitget growth window still contains daily observations;
polling it hourly does not create hourly underlying data.

Successful requests are persisted before calculation consumers can load them.
The current UTC bucket is checked in SQLite, so process restarts do not repeat
successful feeds. Failed sources remain eligible for retry at the next wake-up.
Retries share the original bucket's collection identity, so a recovered source
can complete its GEX input group. A failed source never discards the others.
Missing option chains and order books cannot be backfilled.

Compact storage is the default: exact UTF-8 responses are zlib-compressed and
keyed by SHA-256, while each receipt retains its source, request time, availability
time and collection identity. Identical response bytes share payload storage.
`--storage-mode raw` remains available for diagnostics and existing readers.
Old raw tables coexist with compressed tables; the independent reader handles
both. Legacy OHLCV/authenticated-margin rows are not calculation inputs.

The rolling retention default is **24 hours**, measured by receipt time. Set
`--retention-hours` to a larger positive value for longer research history.
Expired observations and unreferenced compressed payloads are removed after
polls. SQLite reuses free pages; pruning does not promise that an existing file
immediately shrinks. Existing expired records are subject to this retention.

Collection also runs alongside bot processes, using the same schedule and compact
storage. A shared collection lock prevents simultaneous standalone and bot-owned
collectors from duplicating successful polls. Background collection errors do not
stop trading.

### Independent storage and VPS download

The sibling `../storage/` project owns SQLite persistence, causal queries,
retention, online backup and SSH transfer. It has no production dependencies on
bot or calculator code. The application lockfile installs it as a local package;
copy `storage/` alongside `server_bot/` and `install/` when deploying.

From the deployment root on your local machine:

```bash
uv sync --locked --project storage
uv run --locked --project storage python -m market_storage pull \
  --host popoviktorv@YOUR_VPS \
  --remote-root /home/popoviktorv/paper_reference_bot \
  --output ./downloads/tunnel_inputs.sqlite
```

The remote root must be absolute. SSH uses your existing configuration and agent
in noninteractive mode. The remote storage package creates a consistent SQLite
online backup, including committed WAL data, while collection continues. The
local download is streamed to a temporary file, validated, and atomically
published. Transfer or validation failure preserves the previous local file.
A pull refreshes the local snapshot; it does not merge archives or change the VPS.

`market_storage.inputs.load_inputs(Archive(path=...), window)` reads only committed
successful observations available by the window cutoff. It exposes complete GEX
collections and independent oscillator streams. Exchange field schemas are still
validated by calculation consumers.

In the sibling calculator project, the archive-first API is:

```python
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gui_calculator.archived_market import calculate_archive
from market_storage.models import Window

end = datetime.now(UTC)
result = calculate_archive(
    Path("../paper/server_bot/downloads/tunnel_inputs.sqlite"),
    Window(asset="BTC", start=end - timedelta(hours=24), end=end),
)
```

This calculates GEX strike exposures, order-book imbalance, hourly loan-growth
momentum and five-minute OI divergence entirely from stored inputs. Incomplete
GEX groups are omitted, malformed fields fail validation, and no live requests
fill gaps. OI pairs recorded ticker prices from the matching five-minute bucket
with received OI data, retaining actual availability times. These are sampled
prices, not completed candle closes. The 12-hour OI lookback is preserved; price
and OI gaps require warmup again. Hourly live-dashboard behavior is unchanged.

### Updating a VPS installation

After committing and pushing, run on the VPS:

```bash
cd ~/paper_reference_bot
git pull --ff-only
uv sync --locked --project server_bot
uv sync --locked --project storage
systemctl --user restart paper-gex-archive.service
journalctl --user-unit=paper-gex-archive.service -n 20 --no-pager
```

Inspect `systemctl --user cat paper-gex-archive.service` for old overrides. Change
any `--poll-seconds 600` setting to `--poll-seconds 300`; longer wake-ups are now
rejected so the five-minute schedule cannot silently become ten-minute sampling.
After editing a unit, run `systemctl --user daemon-reload` before restarting.
A healthy pass reports `{"failed_sources": 0}`; subsequent same-bucket runs can
successfully do no work because all due inputs have already been committed.

### Diagnosing a failed source

The collector logs the source, exception type, HTTP status and numeric API
code before the failure count. It never logs exception messages, URLs, headers
or credentials. Raw response bodies remain in the archive for inspection.
A failed source does not discard other sources or stop later polls.
