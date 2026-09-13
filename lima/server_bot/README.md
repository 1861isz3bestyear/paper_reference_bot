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
`https://api-demo.bybit.com`. It owns separate state and instance-lock files and follows
the reference process through its atomic `reference_target.json` snapshot. Use API credentials created inside Bybit's Demo Trading
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
