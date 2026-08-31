# Bot service installer

This utility manages the reference paper bot, live paper bot, and Bybit Demo bot as Debian user
systemd services. Run it from this directory:

```bash
uv run --project ../server_bot python bot_services.py install
uv run --project ../server_bot python bot_services.py check
uv run --project ../server_bot python bot_services.py status
uv run --project ../server_bot python bot_services.py collect-logs --since "7 days ago"
```

Before installation, stop processes launched with the legacy background command or tmux. The
installer validates the unit files, enables all three services, and starts them with `--resume`.
It never resets bot state. Enable user lingering once so services survive logout and reboot:

```bash
sudo loginctl enable-linger "$USER"
```

Other commands are `start`, `stop`, `restart`, and `uninstall`. `collect-logs` creates a timestamped
`.tar.gz` containing all three journals, service status, bot status, and statistics. Credential-like
assignments in captured output are redacted.

## Required credential files

Debian does not need a special `.env`. The existing application files are used directly:

- `../server_bot/bybitapi.env`: read-only mainnet Bybit key used by both simulated paper bots to
  retrieve account fee rates.
- `../server_bot/bybitapidemo.env`: Bybit Demo Trading key used for demo orders.

Create them from their `.example` files and restrict permissions with `chmod 600`. Do not use
Testnet credentials for the Demo Trading service.
