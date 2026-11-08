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

Install `curl` if the VPS image does not include it, install `uv` as the service user, and create
the environment:

```bash
sudo apt-get update
sudo apt-get install -y curl
cd "$HOME/server-bot/install"
command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
uv sync --project ../server_bot
```

If the project is installed elsewhere, substitute its actual `install` path. `uv` obtains the
Python version required by the project, so Debian 11's older system Python is not a blocker.

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

Run the installer from the deployment's `install` directory:

```bash
uv run --project ../server_bot python bot_services.py install
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

Valid entities are `reference`, `paper`, `demo`, `smtp`, and `bybit`. Real-money Bybit is deliberately
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
