# Server Bot

Runs two anchored VWAP paper bots from one repository and the exact `paper_bot_config.json` exported by the `anchored_vwam_backtest` calculator:

- `reference_bot` is sourced from `anchored_vwam_backtest/paper_trading_bot`.
- `live_paper_bot` is sourced from `anchored_vwam_paper_bot/live_paper_trading_bot`.

Both bots receive identical strategy and market settings. Their ledgers, cursors, candle caches, logs, PIDs, and instance locks are separate, so they can run concurrently.

## Install

For selectable Debian user-systemd installation and automated diagnostic archives, use the
centralized [`../install`](../install/README.md) utility.

```bash
uv sync
uv run pytest
```

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
`https://api-demo.bybit.com`. It owns separate state and instance-lock files and does not
depend on either paper process. Use API credentials created inside Bybit's Demo Trading
environment in `bybitapidemo.env` (Bybit Testnet keys are not compatible):

```bash
cp bybitapidemo.env.example bybitapidemo.env
chmod 600 bybitapidemo.env
```

Run it directly in the foreground:

```bash
uv run python -m bybit_demo_bot.cli run --resume \
  --config ./paper_bot_config.json --env ./bybitapidemo.env
```

The equivalent combined CLI command is:

```bash
uv run python -m server_bot.cli run-bybit-demo --resume \
  --config ./paper_bot_config.json --env ./bybitapidemo.env
```

The paper bots and demo bot support non-reversed `Bybit REST` configurations for
`BTC_USDT`, `XRP_USDT`, `DOGE_USDT`, `ADA_USDT`, `TRX_USDT`, `LINK_USDT`, `AVAX_USDT`,
`DOT_USDT`, `TON_USDT`, and `NEAR_USDT`. The demo bot reads completed
Bybit candles and uses `reference_bot`'s target-side calculation. Entries are sized from
90% of the demo account's available USDT using exchange quantity and notional limits.
The demo runner uses reference's persisted launch timestamp when `reference_state.json`
is present at startup, aligning the strategy start and VWAP anchor. Otherwise it retains
its own launch timestamp. Both processes must use the same configuration for comparison.

Demo entries and ordinary exits use completed-candle decisions and market orders. There
is no additional VWAP TP entry filter or consumed-direction re-entry guard: after an
exchange close, the bot may re-enter on the next unprocessed completed candle if the
strategy still requests that side. Exchange VWAP TP is explicitly cleared on protection
updates, including existing positions after upgrade. The configured exchange SL remains
(0 disables it). Failure to install initial protection attempts an emergency close and halts.
Pending entry submissions block additional entries until a position is observed; an
ambiguous or rejected submission with no resulting position requires operator review.
Reversals wait for an observed flat exchange position before opening the opposite side.

This aligns trading rules, not exact fills or account balances: the exchange SL can fire
intraminute, market fills and funding differ, and reference uses its own simulated capital.
After updating the server checkout, restart `bybit-demo.service` with the existing resume
state. No account reset is required. These changes do not alter mainnet's execution policy.

Use the centralized [`../install`](../install/README.md) utility for the supported user-systemd
service; do not maintain a separate hand-written unit.

## Real-money Bybit executor

`bybit_bot` uses the shared strategy and legacy execution policy, 90%-of-available-USDT sizing, and mandatory
exchange-side protection mechanics as `bybit_demo_bot`, but sends orders to Bybit mainnet at
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
