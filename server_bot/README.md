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

## Bybit demo-account executor

`bybit_demo_bot` runs the same shared strategy configuration against Bybit Demo Trading at
`https://api-demo.bybit.com`. It owns separate state and instance-lock files and follows
the reference process through its atomic `reference_target.json` snapshot. Use API credentials created inside Bybit's Demo Trading
environment in `bybitdemoapi.env` (Bybit Testnet keys are not compatible):

```bash
cp bybitapidemo.env.example bybitdemoapi.env
chmod 600 bybitdemoapi.env
```

Run it directly in the foreground:

```bash
uv run python -m bybit_demo_bot.cli run --resume \
  --config ./paper_bot_config.json --env ./bybitdemoapi.env
```

The equivalent combined CLI command is:

```bash
uv run python -m server_bot.cli run-bybit-demo --resume \
  --config ./paper_bot_config.json --env ./bybitdemoapi.env
```

The paper bots and demo bot support non-reversed `Bybit REST` configurations for
`BTC_USDT`, `XRP_USDT`, `DOGE_USDT`, `ADA_USDT`, `TRX_USDT`, `LINK_USDT`, `AVAX_USDT`,
`DOT_USDT`, `TON_USDT`, and `NEAR_USDT`. Reference publishes its actual position,
quantity, entry identity, configuration fingerprint, and heartbeat after each successful
processing cycle. Demo polls at most every two seconds and copies the latest position
with market orders. It does not calculate its own signals, allocate 90% of its balance,
or apply an independent re-entry policy. Both services must use the same configuration.

**Reference controls all normal exits, including its configured SL and VWAP-band exits.**
Demo clears exchange SL and TP instead of creating independent stop levels from its own
fill price. Actual fills, fee rates, funding and account percentage returns can still differ.
Demo must have sufficient funds for the exact reference quantity; it halts rather than
silently scaling an entry. Use a dedicated demo account/position for this executor.

A saved order link identifies each submission. Demo confirms both terminal order status
and the resulting exchange position before submitting the next action. Pending/partial
fills block additional orders. An unresolved order after 60 seconds triggers a persistent
halt, cancellation attempts, and emergency flattening. A missing/invalid reference,
configuration mismatch, heartbeat older than 120 seconds, or candle cursor older than
three intervals plus 30 seconds also halts and attempts to flatten. Initial startup while
flat allows 120 seconds for the first snapshot. Halts remain recorded in `halted_reason`
for the SMTP monitor; investigate outstanding orders before clearing a halt. Failed
emergency closes are retried while the process runs. **This software failsafe cannot close
positions while the VPS/demo process is stopped or the exchange is unreachable.**

On upgrade, a matching existing position is adopted and old SL/TP removed. A mismatched
side or quantity is closed before opening the reference target, so migration can incur
an extra round trip. Reference close/reopen events are distinguished by entry identity,
even if the side and quantity are unchanged. After downtime, demo reconciles the latest
position; it does not replay historical trades. Unexpected external closures while
reference still holds halt the replica instead of creating a repeated re-entry loop.

### Upgrade or start with fresh reference accounting

Update the server files first. This version requires restarting **reference as well as
demo**, because reference now publishes the target snapshot. Keep `--resume` and the
existing demo state so outstanding orders remain identifiable.

```bash
systemctl --user stop paper-bot-reference.service bybit-demo.service
cd ~/paper_reference_bot/lima/server_bot

# Optional: deletes reference's simulated account/history and creates a new start.
# Omit this line to preserve reference history.
uv run python -m reference_bot.cli reset

systemctl --user start paper-bot-reference.service
```

Check `reference_target.json` exists and its `published_at` and `reference_run` are current
before starting demo (inspect reference logs if the file has not appeared):

```bash
cat reference_target.json
systemctl --user start bybit-demo.service
systemctl --user status paper-bot-reference.service bybit-demo.service --no-pager -l
journalctl --user-unit=paper-bot-reference.service --user-unit=bybit-demo.service --since "5 minutes ago" -f
```

Resetting reference does not erase Bybit's transaction history, reset its balance, or
close exchange positions while the services are stopped. On resume, demo reconciles the
new reference target, including closing an existing position if reference is flat. Never
delete `bybit_demo_state.json` merely to reset accounting. No mainnet execution policy is
changed by this replica upgrade.

Use the centralized [`../install`](../install/README.md) utility for the supported user-systemd
service; do not maintain a separate hand-written unit.

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

### Recovering a replica halt without resetting accounts

Heartbeat freshness is checked using the time after reading the reference snapshot,
not the time before the Bybit position request. Heartbeat errors include measured age,
publish time, and check time. Halts remain persistent; healthy reference data alone does
not clear them. Emergency cleanup refreshes the saved observed position, while preserving
any unresolved order intent.

After installing the fix, leave reference running and stop only demo:

```bash
systemctl --user stop bybit-demo.service
cd ~/paper_reference_bot/lima/server_bot
uv run python -m bybit_demo_bot.recover
```

Recovery takes demo's instance lock, checks that the exchange position is flat and there
are no open orders for its symbol, verifies any recorded order is terminal, and validates
a fresh matching reference snapshot. It refuses unresolved legacy entries. Only after
all checks succeed does it back up the state and clear the halt and obsolete position
identity. It neither submits nor cancels orders and does not reset either account's history.
Use `--config` and `--env` if the service uses nondefault paths. If a check fails, the saved
halt is retained; resolve the reported condition rather than deleting the state file.

After a successful recovery:

```bash
systemctl --user start bybit-demo.service
journalctl --user-unit=bybit-demo.service --since "5 minutes ago" -f
```

Demo then follows the current reference target; it does not replay trades missed while halted.

### September 2026 calculation and heartbeat corrections

The shared strategy now uses prior-candle bands and requires an actual traded
entry price, handles opening gaps at exits, charges adverse entry-candle stops,
and includes both fees in short P&L. Calculator and server strategy sources are
kept identical. See the calculator's `REALISM_AND_TESTING.md` for assumptions;
latest reference fills and Bybit market fills still differ from intrabar backtest
fills. The calculator now selects Bybit linear perpetual data rather than spot.

Reference and live-paper history validation explicitly converts pandas timestamps
to nanoseconds before computing epoch milliseconds. This avoids rejecting complete
history when pandas stores timestamps in microseconds or milliseconds. Genuine
candle gaps still prevent cursor advancement and target publication.

A saved reference halt now reports whether the **current** reference is healthy.
Repeated unchanged flat-halt messages are limited to once per minute; exchange
reconciliation continues every poll. Healthy data does not automatically clear
an uncertain order or a persisted halt: use the checked recovery procedure above.

Update reference and executor together. The target configuration fingerprint now
includes the revised execution model, so an old-engine snapshot is rejected even
when the JSON configuration matches. Do not delete account/history files to clear
a halt. This code change does not repair old trade records or prove the cause of
the September 15 VPS interruption without the reference journal.

Validation: `uv run pytest` passes 315 tests, including timestamp resolutions
(ns/us/ms), genuine gaps, stale/healthy diagnostic transitions, checked recovery,
old-engine snapshot rejection, and throttled logging without reduced polling.

The default suite also includes local HTTP protocol tests and full reference-to-demo
workflows using real SQLite caches, ledger files, target publication, and demo state.
They cover restart idempotence, delayed fills, accepted orders with lost responses,
history failures, stale halts, checked recovery, and concurrent target reads.
See [workspace testing instructions](../../../../TESTING.md) for the root-level
`pytest` command and test-level selection.

The calculator and server `shared/` directories are identical. Both default pytest
suites verify their filenames and file contents byte-for-byte, ignoring generated
cache files. Apply shared changes to both projects together.

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

### Snapshot tunnel input archive

Run the independent data collector from this directory:

```bash
uv run python -m server_bot.tunnel_archive --output tunnel_inputs.sqlite
# One collection pass (nonzero exit if a source fails):
uv run python -m server_bot.tunnel_archive --once
```

It records BTC (or `--asset ETH`) Deribit option instruments, option summaries
and index price; Bybit top-50 linear depth, native one-minute OHLCV candles,
current ticker price and open interest, hourly candle/open-interest warmup
history, and USDT margin-rate history. Set `BYBIT_API_KEY` and
`BYBIT_API_SECRET` with margin read access for the authenticated rate endpoint.
The collector never submits orders. The default polling interval is 60 seconds;
use `--poll-seconds` to change it.

SQLite `tunnel_inputs` retains each raw JSON response, source, asset, request
time, receipt/availability time and error type, including repeated observations
across restarts. New rows also have a `collection_id` and UTC `minute_at`
label so all sources in a pass can be joined without mixing separate polls.
Existing rows are preserved with null collection/minute labels. The minute
label is the collection start minute; actual receipt times determine when
data can be used. Each source commits independently, so unavailable margin data
cannot discard options or depth. Failed HTTP responses retain their bodies.
Credentials and request headers are not stored.

This is a raw archive, not the GUI snapshot database or a trading runner.
Replay consumers must validate payload rows, join instrument definitions with
summaries, and use receipt times to avoid future knowledge. Each pass saves the
latest API window (1000 candles per interval and 200 hourly OI points); it does
not backfill gaps or paginate historical OI. Current candles may be unfinished.
Chain requests are sequential observations, not an atomic exchange snapshot.
Continuous collection is required for historical option chains and depth.

The archive also starts automatically alongside every reference, live-paper,
Bybit demo and mainnet bot run, including launches through `server_bot.cli start`
and the installed systemd services. It writes BTC inputs to
`tunnel_inputs.sqlite` in the server project directory every 60 seconds.
A shared `tunnel_inputs.lock` serializes polls and preserves the polling cadence
across bot restarts. Collection runs in a background thread and stops with its
bot; another running bot continues polling. Archive errors are logged without
interrupting trading. The archive survives bot resets. Margin credentials must
be present in the process environment; executor-specific env files are not
implicitly reused for the mainnet margin endpoint.

Automatic collection begins immediately and subsequent polls target UTC minute
boundaries, without adding a full minute after each request cycle. Native price
candles retain their exchange timestamps and OHLCV values. Open interest, depth,
margin rates and option chains are minute observations, not synthetic OHLC
candles; their native update frequency may be slower. Keep hourly history for
indicator warmup. GEX replay uses instrument expiry/strike/type/contract size,
option IV and open interest, and index price from the raw option responses.
A missing source remains an explicit error, not a carried-forward value.
Exchange outages or collection passes taking over a minute can leave gaps;
historical option chains and order books cannot be backfilled by this collector.

### Compact archive storage

Use the optional compact mode for ten-minute snapshots:

```bash
uv run python -m server_bot.tunnel_archive --asset BTC \
  --storage-mode compact --poll-seconds 600 \
  --output tunnel_inputs_compact.sqlite
```

The default remains `--storage-mode raw` with a 60-second interval. Compact
mode stores UTF-8 response text compressed with zlib in `tunnel_payloads`,
keyed by its SHA-256 digest. Identical response bodies share one payload,
including unchanged instrument definitions. `tunnel_observations` retains
one row per source per pass, with asset, source, request/availability timestamps,
error, collection ID, minute label and payload digest. Each source commits its
payload and observation atomically. Failed response bodies are retained too.

Join observations to payloads on `digest`, then decode with
`zlib.decompress(payload).decode("utf-8")` to recover the original response text.
Existing raw-archive readers do not read these new tables. Use a separate output
file for clarity; selecting a mode does not migrate or delete existing data.
The schema can coexist with raw tables in the same database.

Compression is lossless and deduplication compares exact response text, so
exchange timestamps or reordered JSON can prevent sharing. Full candle/OI
windows are still collected; this mode does not implement incremental history
or retention. Ten-minute polling reduces snapshot count by 90% but misses
changes between snapshots. Measure actual disk growth after successful
Deribit collection before sizing retention; no compression ratio is guaranteed.

For the installed GEX service, run
`systemctl --user edit paper-gex-archive.service` and set:

```ini
[Service]
ExecStart=
ExecStart=/home/USER/.local/bin/uv run python -m server_bot.tunnel_archive --asset BTC --storage-mode compact --poll-seconds 600 --output tunnel_inputs_compact.sqlite
```

Replace the uv path with the absolute executable path from the installed unit
(`systemctl --user cat paper-gex-archive.service`). Keep its working directory.
Then run `systemctl --user daemon-reload` and
`systemctl --user restart paper-gex-archive.service`. This changes only the
standalone GEX collector; bot-owned automatic collection still uses raw mode.
