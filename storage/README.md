# Market input storage

Independent Python package for exact market-input persistence, compressed payload
sharing, causal reads, rolling retention and consistent VPS-to-local downloads.
Production code uses only Python's standard library. Bot collection and indicator
calculations depend on this package; it imports neither of them.

Deploy this directory alongside `server_bot/` and `install/`. The server's local
path dependency requires all three siblings in the checkout.

## Download from a running VPS

From the deployment root on the local machine:

```bash
uv sync --locked --project storage
uv run --locked --project storage python -m market_storage pull \
  --host popoviktorv@YOUR_VPS \
  --remote-root /home/popoviktorv/paper_reference_bot \
  --output downloads/tunnel_inputs.sqlite
```

The VPS needs the same `storage/` project installed. SSH must already work with
an agent/key or SSH configuration; batch mode does not ask for passwords. The
remote command finds uv in PATH or `$HOME/.local/bin`. Use `--remote-database`
for an alternate archive path relative to the deployment root.

The remote process exports an online SQLite backup, including committed WAL
contents. The downloader streams to a temporary local file and checks database
integrity, schema and payload references before atomic replacement. A failure
leaves the prior destination intact. Each pull replaces the local snapshot;
it does not merge historical downloads or change the VPS database.

A standalone export, useful for other transports:

```bash
uv run --locked --project storage python -m market_storage export \
  --archive server_bot/tunnel_inputs.sqlite > snapshot.sqlite
```

## Read before calculating

```python
from datetime import UTC, datetime, timedelta
from pathlib import Path

from market_storage.archive import Archive
from market_storage.inputs import load_inputs
from market_storage.models import Window

end = datetime.now(UTC)
inputs = load_inputs(
    Archive(path=Path("downloads/tunnel_inputs.sqlite")),
    Window(asset="BTC", start=end - timedelta(hours=24), end=end),
)
```

The reader supports raw and compact tables. It excludes failed responses,
observations received after the cutoff, and legacy OHLCV/authenticated-margin
sources. A GEX packet requires instruments, option summaries and index price from
the same successful collection. The packet becomes available at the latest
receipt time of its members. Ungrouped legacy raw rows receive separate read-only identities, preserving
their original times without joining unrelated rows into a GEX packet.
Individual response fields remain an external-data
boundary and are validated by the calculation adapter.

The collector uses this package with five-minute market buckets and hourly
lending buckets. Successful source requests complete their bucket across
restarts; failed requests can retry. Retention defaults to 24 hours in the
collector. Old rows and orphan payloads are deleted; SQLite can reuse free pages
without immediately reducing the existing file size.

The calculator's `gui_calculator.archived_market.calculate_archive` API loads this
archive before computing GEX and oscillator values. It never falls back to live
HTTP. Five-minute OI uses stored ticker prices, not OHLCV; the original 12-hour
lookback remains. The current GUI's live data controls are separate from this
archive API.

## Verification

```bash
cd storage
uv sync --locked
uv run pytest
```

Unit tests validate domains and CLI arguments; module tests use real SQLite;
integration tests verify backup, WAL visibility, causal loading and atomic
replacement; E2E tests execute both local and remote export CLIs. The SSH network
edge is replaced by a fixture executing the actual remote command locally.
These tests do not establish connectivity or authentication to a particular VPS.
Collector E2E tests additionally use a real local HTTP exchange fixture.

CI gates unit, module, integration and E2E stages in that order across both the
storage and application projects. A failing stage blocks all following test
stages. Coverage from each stage is combined, preserving the application's 92%
combined line/branch gate and adding an independent 92% gate for `market_storage`.

## One-command dated downloads

Install the standalone command on your local machine (Python 3.14 is obtained
by uv when needed):

```bash
uv tool install --python 3.14 /absolute/path/to/paper/server_bot/storage
```

Add these settings to your local shell profile, filling in the SSH address later:

```bash
export GEX_HOST="popoviktorv@YOUR_VPS_IP"
export GEX_REMOTE_ROOT="/home/popoviktorv/paper_reference_bot"
export GEX_REMOTE_DATABASE="server_bot/tunnel_inputs_compact.sqlite"
export GEX_OUTPUT_DIR="$HOME/gex-downloads"
```

Use the actual database filename from the service's `ExecStart`; the default
without configuration is `server_bot/tunnel_inputs.sqlite`. After opening a new
terminal, run:

```bash
gex-pull
```

Alternatively run `uv run --locked --project storage gex-pull` from the deployment
root. Explicit `--host`, `--remote-root`, `--remote-database` and `--output-dir`
options override environment settings. SSH must already work noninteractively.

Each run retains a unique UTC-dated `.sqlite` snapshot and a lossless `.sqlite.gz`
copy. Transfer validates database integrity, schema and payload references before
publication. Compression is streamed and published atomically; if it fails, the
validated SQLite copy remains and the command exits nonzero. No local snapshots
are automatically deleted or merged. Keeping both copies requires space for both;
the VPS also needs temporary space for an online SQLite backup. Compact payloads
are already compressed, so gzip may offer limited additional savings.

Use SQLite files for calculation; decompress archived gzip files before use.
Storage validation does not establish that every indicator has sufficient data
or run the GUI calculator. The storage package deliberately imports no calculator
code. The current GUI live controls do not load these snapshots directly.

For unattended downloads, schedule `gex-pull` every 12 hours with macOS launchd
(or cron/systemd on Linux). Configure the four environment settings in the job
itself: schedulers do not read your interactive shell profile. Use absolute paths
for the installed executable and output directory, retain stdout/stderr logs, and
ensure SSH keys and known hosts work in the scheduled environment. A sleeping or
offline Mac cannot download; the VPS's default 24-hour retention can therefore
leave gaps. Scheduling is not installed automatically.
