# Server Bot

Runs two anchored VWAP paper bots from one repository and the exact `paper_bot_config.json` exported by the `anchored_vwam_backtest` calculator:

- `reference_bot` is sourced from `anchored_vwam_backtest/paper_trading_bot`.
- `live_paper_bot` is sourced from `anchored_vwam_paper_bot/live_paper_trading_bot`.

Both bots receive identical strategy and market settings. Their ledgers, cursors, candle caches, logs, PIDs, and instance locks are separate, so they can run concurrently.

## Install

```bash
uv sync
uv run pytest
```

Copy and edit the shared configuration if needed:

```bash
cp paper_bot_config.example.json paper_bot_config.json
nvim paper_bot_config.json
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

## Real-money MEXC executor

The optional foreground command waits until `reference_bot` and `live_paper_bot` have
reached the same completed candle, then calculates the anchored VWAP and standard
deviation from the reference candle cache. Missing, stale, or conflicting candle state
causes no order. It supports non-reversed, trend-mode `MEXC REST` VWAP-band configurations.

Copy the local environment template and restrict it:

```bash
cp .env.example .env
chmod 600 .env
nvim .env
```

Start both paper services first, then run the real executor in the foreground:

```bash
uv run python -m server_bot.cli run-real --confirm-live \
  --config ./paper_bot_config.json --env ./.env
```

After each completed candle the executor performs these operations in order:

1. Recalculate AVWAP and sigma bands.
2. Monitor MEXC's live last price locally.
3. At `open_sigma_1`, submit one Long market entry.
4. At `open_sigma_2`, submit one Short market entry.

The example config uses `+1σ → +2σ` and `-1σ → -2σ`. MEXC rejects private plan-order
placement for this account, so crossover detection runs in the executor. Only the side
that crosses is submitted; there is no resting sibling order. MEXC rejects SL/TP attached
to these market entries, so submission intent and its protection plan are persisted before
the API call for timeout/crash recovery. Immediately after the fill appears, the executor
installs protection through MEXC's stop-order endpoint. If protection
cannot be confirmed, it submits an emergency market close and permanently halts new
entries until an operator clears the state after inspecting the account.

`initial_capital` is the maximum USDT notional per entry. Prices and volumes are rounded
down to MEXC contract steps, and orders below `0.01 USDT` are rejected. Because only one
entry is submitted, it is capped at 98% of available margin (converted to notional using
configured leverage), leaving 2% for fees and rounding.
Each stop loss is calculated from the actual rounded order deposit (notional divided by
configured leverage): the maximum modeled loss is `deposit × stop_loss_pct`. Each take
profit is the corresponding `close_sigma` price.

Example `/etc/systemd/system/server-bot-real.service` (replace paths and user):

```ini
[Unit]
Description=Server Bot MEXC real-money executor
After=network-online.target reference-bot.service live-paper-bot.service
Wants=network-online.target
Requires=reference-bot.service live-paper-bot.service

[Service]
Type=simple
User=bot
WorkingDirectory=/opt/server_bot
ExecStart=/usr/local/bin/uv run python -m server_bot.cli run-real --confirm-live --config /opt/server_bot/paper_bot_config.json --env /opt/server_bot/.env
Restart=on-failure
RestartSec=10
TimeoutStopSec=30
KillSignal=SIGTERM
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Then use `systemctl daemon-reload`, `systemctl enable --now server-bot-real`, and
`journalctl -u server-bot-real -f`.

## Health command for external supervision

`health-check` is read-only and contains no systemd or restart behavior. It compares both state files and returns:

- exit `0`: reference is fewer than 10 candles behind live-paper;
- exit `1`: live-paper is current but reference is at least 10 candles behind;
- exit `2`: health is indeterminate because live-paper itself is stale or state is unavailable.

Thresholds can be changed with `--stale-candles` and `--paper-grace-candles`. A systemd health service or timer can later use these exit codes to decide whether to restart the reference service.
