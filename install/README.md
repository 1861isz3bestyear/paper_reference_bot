# Bot service installer

This utility manages the reference paper bot, live paper bot, Bybit Demo bot, SMTP health monitor, and optional
real-money Bybit mainnet bot as Debian user systemd services. It supports Debian 11 and Debian 12 on a VPS, physical server, or Lima VM. Both
Debian releases have passed the post-install integration suite on ARM64; the installer is not
specific to Lima.

The deployment directory must contain these siblings:

```text
deployment/
├── install/
│   ├── bot_services.py
│   └── test_installed_services.py
└── server_bot/
    ├── pyproject.toml
    └── uv.lock
```

## Debian 11/12 VPS setup

Copy or check out the project on the VPS under the account that will run the bots. Use a writable
local filesystem, for example `$HOME/server-bot`; the application writes state, lock, and SQLite
files beside its source. Do not copy a `.venv` from macOS or another machine.

Install the download prerequisites if needed:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
cd "$HOME/server-bot/install"
```

Run `sh install.sh` below as the account that will run the services. It reuses uv from
`PATH` or `$HOME/.local/bin`, or installs uv 0.13.7 into `$HOME/.local/bin` when
missing. It then syncs locked dependencies and installs the services. No existing
Python installation is required: uv obtains the project's required Python version.
The bootstrap does not modify shell profiles. For later `uv` commands, add
`$HOME/.local/bin` to `PATH`, for example `export PATH="$HOME/.local/bin:$PATH"`.
If the project is installed elsewhere, substitute its actual `install` path.

Enable lingering once per VPS so the user services start at boot and survive SSH logout.
Persistent journaling and membership in `systemd-journal` allow the service user to collect logs
and run the integration test:

```bash
sudo loginctl enable-linger "$USER"
sudo mkdir -p /var/log/journal /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=persistent\n' | \
  sudo tee /etc/systemd/journald.conf.d/persistent.conf
sudo systemd-tmpfiles --create --prefix /var/log/journal
sudo usermod -aG systemd-journal "$USER"
sudo systemctl restart systemd-journald
```

Log out of SSH and reconnect so the new group membership takes effect, then confirm that `id`
lists `systemd-journal`. These are one-time server setup steps.

## Lima setup

Lima uses the same Debian procedure above. First create and enter a VM; Debian 12 normally uses
Apple's VZ driver, while the Debian 11 template may require QEMU (`brew install qemu`):

```bash
limactl start --name=debian-stable template:debian-12
limactl shell debian-stable
```

Lima exposes the macOS home directory at the same `/Users/...` path in the guest, but that mount
may be read-only. Copy the deployment into the guest's writable home and exclude the macOS virtual
environment:

```bash
mkdir -p "$HOME/server-bot"
tar -C /Users/viktor/projects/ai_generated/codex/paper/server_bot/lima \
  --exclude='./server_bot/.venv' -cf - . | \
  tar -C "$HOME/server-bot" -xf -
cd "$HOME/server-bot/install"
```

Replace the `/Users/viktor/...` source with the actual macOS path. Continue with `uv` installation,
the one-time systemd/journald setup, credentials, installation, and testing described for a VPS.

## Install and manage the services

Configure SMTP monitoring before the default installation:

```bash
cd "$HOME/server-bot/smtp_monitor"
cp smtp_monitor.env.example smtp_monitor.env
chmod 600 smtp_monitor.env
```

The default installation enables `smtp-monitor.timer`. It checks bot health every minute, sends
the first alarm after a problem persists for two minutes, repeats unresolved alarms hourly, and
sends a recovery message. Use `--entity smtp` to manage only the monitor.

Run the installer from the deployment's `install` directory. Bootstrap arguments
are forwarded to `bot_services.py install`, for example
`sh install.sh --entity data-saver --no-start`. The script also works when invoked
by absolute path from another directory. Service setup still requires the one-time
systemd configuration above; the bootstrap does not use sudo or configure lingering.

```bash
sh install.sh
export PATH="$HOME/.local/bin:$PATH"
uv run --project ../server_bot python bot_services.py check
uv run --project ../server_bot python bot_services.py status
uv run --project ../server_bot python bot_services.py collect-logs --since "7 days ago"
```

With no `--entity` options, commands operate on the safe default set: `reference`, `paper`, and
`demo`. Select one or more explicitly by repeating the option:

```bash
# Install only the two paper services.
uv run --project ../server_bot python bot_services.py install \
  --entity reference --entity paper

# Install only Demo Trading.
uv run --project ../server_bot python bot_services.py install --entity demo

# Explicitly install the real-money Bybit mainnet service.
uv run --project ../server_bot python bot_services.py install --entity bybit

# Manage or inspect the same subset.
uv run --project ../server_bot python bot_services.py status --entity demo --entity bybit
uv run --project ../server_bot python bot_services.py collect-logs \
  --entity demo --entity bybit --since "1 day ago"
```

Valid entities are `data-saver`, `gex`, `reference`, `paper`, `demo`, `smtp`, and `bybit`. Real-money Bybit is deliberately
excluded from the default set and can only be installed or started when `--entity bybit` is
given explicitly.

Before installation, stop processes launched with the legacy background command or tmux. The
installer validates the selected unit files, enables the selected services, and starts them with `--resume`.
It never resets bot state.

Other commands are `start`, `stop`, `restart`, and `uninstall`. `collect-logs` creates a timestamped
`.tar.gz` containing the selected journals, service status, bot status, and statistics. Credential-like
assignments in captured output are redacted.

The installer deliberately renders an unquoted absolute `WorkingDirectory`, ignores the `uv run`
launcher when checking for one Python bot process, and queries logs with `journalctl --user-unit`.
These details are required for compatibility with Debian's systemd and journal layout; no manual
patching with `sed` is required.

After a VPS reboot or later VM start, the enabled services should start automatically. The normal
verification workflow is:

```bash
cd "$HOME/server-bot/install"
uv run --project ../server_bot python bot_services.py check
```

## Post-install integration test

After `install` reports success, run the opt-in test against the real user systemd manager:

```bash
RUN_SYSTEMD_INTEGRATION=1 \
uv run --project ../server_bot pytest -q test_installed_services.py
```

The test validates the rendered units, confirms that all services are enabled and active, checks
for exactly one process per bot, restarts the Bybit Demo service, confirms its new PID and startup
journal entry, and creates and inspects a diagnostic archive. The restart uses the installed
`--resume` command and does not reset bot state. Without `RUN_SYSTEMD_INTEGRATION=1`, this suite is
skipped so a normal test run cannot restart a bot accidentally.

## Required credential files

Debian does not need a special `.env`. The existing application files are used directly:

- `../server_bot/bybitapi.env`: read-only mainnet Bybit key used by both simulated paper bots to
  retrieve account fee rates.
- `../server_bot/bybitapidemo.env`: Bybit Demo Trading key used for demo orders.
- `../server_bot/bybitrealapi.env`: dedicated Bybit mainnet trading key used only by the
  explicitly selected real-money service. Disable withdrawals on this key.

Create them from their `.example` files and restrict permissions with `chmod 600`. Do not use
Testnet credentials for the Demo Trading service.

### Install only the public candle data saver

No trading configuration, exchange credentials, account state, or SMTP setup is
required. From the application directory (`server_bot/`):

```bash
sh ../install/install.sh --entity data-saver
export PATH="$HOME/.local/bin:$PATH"
uv run python ../install/bot_services.py check --entity data-saver
```

This installs only `paper-data-saver.service`. It runs
`python -m server_bot.data_saver` with BTCUSDT **1m** candles, a 30-second poll,
and a seven-day initial backfill. The SQLite archive is `data/candles.sqlite`
relative to the application directory. Completed candles are upserted by symbol,
interval and timestamp; a restart resumes from the latest stored candle,
including gaps during downtime. Continuous mode logs failed requests and retries.

For a manual collection or custom settings:

```bash
uv run python -m server_bot.data_saver --once
uv run python -m server_bot.data_saver --symbol ETHUSDT --interval 5m --database data/eth.sqlite --history-days 30
```

For service customization use `systemctl --user edit paper-data-saver.service`
and replace `ExecStart` with the same installed command plus desired arguments.
Use `stop`, `restart`, `uninstall` or `collect-logs` with `--entity data-saver`
to manage this service independently. The saver uses only public Bybit linear
market data and never submits orders. It cannot recover candles no longer
available from the exchange.

### End-to-end CI checks

Both Debian 11 and Debian 12 jobs run
`tests/test_data_saver_e2e.py` and upload `data-saver-e2e.xml` with the other
JUnit reports. To run these checks from the application directory:

```bash
uv run --locked pytest tests/test_data_saver_e2e.py -m e2e --no-cov
```

The tests start separate Python processes and a local HTTP exchange fixture,
then inspect real SQLite files. Only the exchange URL is redirected; production
CLI parsing, HTTP requests, pagination and storage execute normally. Scenarios
cover the default 1m backfill, completed candles, restart and overlap updates,
HTTP/API failure recovery, custom series, empty responses and invalid settings.
The installer test invokes the actual CLI with no trading config or credentials,
checks the generated unit, and verifies install/uninstall command arguments.
Systemd commands are recorded by fixture executables because CI containers have
no user systemd manager. Actual service activation remains covered by the
opt-in `test_installed_services.py` checks on a configured Linux host.

This E2E run supplements the existing full-application and standalone-saver
coverage gates; it does not replace either gate or depend on live exchange data.


## Install only the GEX input archive

From the cloned repository on Debian 11/12, run as the service user:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo loginctl enable-linger "$USER"
cd ~/paper_reference_bot/paper/server_bot/install
sh install.sh --entity gex
export PATH="$HOME/.local/bin:$PATH"
uv run --project ../server_bot python bot_services.py check --entity gex
journalctl --user-unit=paper-gex-archive.service -f
```

The bootstrap obtains uv, Python and locked dependencies and enables and starts
only `paper-gex-archive.service`. No trading configuration, trading bots, SMTP
monitor or credential files are required. The default entity selection is unchanged.

This service runs `server_bot.tunnel_archive --asset BTC` and saves raw inputs to
`../server_bot/tunnel_inputs.sqlite` every minute. It collects Deribit option
instruments, summaries (including IV and open interest), and index prices needed
for GEX replay, plus Bybit market inputs. It does not calculate GEX. The additional
Bybit margin-rate endpoint requires mainnet margin-read credentials in the process
environment; without them that source records errors while public sources continue.
The candle-only `data-saver` entity does not archive option-chain inputs.

Use `status`, `stop`, `restart`, `uninstall`, or `collect-logs` with `--entity gex`
to manage this service. Uninstalling preserves the archive. For ETH, override
`ExecStart` with `systemctl --user edit paper-gex-archive.service` and use
`--asset ETH` with a separate `--output` path.

If you previously pointed `paper-data-saver.service` at the tunnel archive using
a manual override, stop and disable that service before installing `gex` to avoid
duplicate collection. Remove that manual override before reusing the candle service.
