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
integrity, schema and payload references before atomic replacement. Transfer or validation failure leaves the prior destination intact. Pull now
merges into cumulative local history instead of replacing it with the VPS window.
After durable publication it acknowledges only downloaded record fingerprints
on the VPS. An acknowledgement failure reports failure but keeps the local copy;
retry the same command safely. See the checked-history contract below.

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

## One-command cumulative downloads

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

Each run merges into `tunnel_inputs.sqlite` and refreshes its lossless
`.sqlite.gz` copy. Transfer validates metadata and payload contents before
publication. Compression is streamed and published atomically; if it fails, the
validated SQLite copy remains and the command exits nonzero. Old dated snapshots
remain untouched. Keeping both copies requires space for both;
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


## Checked cumulative history and copy receipts

Deploy the updated `storage/` package to **both** the local machine and VPS.
Restart the VPS collector after upgrading so its running Python process uses
copy-aware pruning. This change does not deploy or restart your VPS for you.
Before the first download, activate copied-only retention on the VPS (the online
export performs schema/integrity validation before switching the policy):

```bash
cd /home/popoviktorv/paper_reference_bot
export PATH="$HOME/.local/bin:$PATH"
uv run --locked --project storage python -m market_storage export \
  --archive server_bot/tunnel_inputs.sqlite --copy-managed > /dev/null
systemctl --user restart paper-gex-archive.service
```

Then, on the local machine, from the directory containing `storage/`:

```bash
uv run --locked --project storage python -m market_storage pull \
  --host popoviktorv@YOUR_VPS \
  --remote-root /home/popoviktorv/paper_reference_bot \
  --output downloads/tunnel_inputs.sqlite
```

`gex-pull` also uses one cumulative `tunnel_inputs.sqlite` in its output directory
and refreshes its `.sqlite.gz` copy. Old dated downloads are not deleted or imported
automatically. Merge each existing snapshot explicitly to recover its history:

```bash
uv run --locked --project storage python -m market_storage merge \
  --source old-download.sqlite --output downloads/tunnel_inputs.sqlite
uv run --locked --project storage python -m market_storage check \
  --archive downloads/tunnel_inputs.sqlite
```

The union preserves old local observations even when absent from newer downloads.
Identity is `(asset, source, requested_at, available_at, error)` after UTC
normalization. Identical payloads for that identity collapse, across raw/compact
storage and collection IDs; the first collection identity/minute is retained.
Different payloads for the same identity reject the complete merge. Different
receipt times remain distinct observations, including legitimate revisions.
Failed-response records remain stored but are never successful coverage.

Validation checks SQLite structure, required columns, metadata types, supported
asset/source, timezone offsets, causal timestamps, collection identity, zlib codec,
UTF-8, payload references and SHA256 digests. Successful bodies must parse as JSON.
Provider-specific field validation still belongs to collectors and consumers:
valid JSON and a correct digest cannot prove the exchange data is truthful.
Corruption is rejected, never silently repaired or discarded. Keep the bad file
for investigation. Unsupported legacy rows require an explicit migration.

The JSON report lists unique observations, removed duplicates and per-source
successful/missing UTC buckets (five minutes or one hour). Bounds cover each
asset's first through last observed request, including failed requests. Entirely
missing sources are reported within those bounds. Nothing is forward-filled,
interpolated or deleted to hide a gap. Periods outside those bounds, and missed
polls within an otherwise successful bucket, cannot be inferred by this audit.
An empty archive has no inferred timeline. Gaps do not mean byte corruption and
are reported without rejecting otherwise valid history.

Local publishers use a persistent `.sqlite.lock`, a private staged union, a SQLite
transaction, file fsync, atomic rename and parent-directory fsync. Concurrent
imports serialize. Use the destination as a published read-only calculation
archive, not an actively written SQLite database with WAL sidecars. Local
history has no automatic expiry; monitor disk usage.

After durable local publication, the client sends fingerprints of **only the
received snapshot**, never a timestamp cutoff or the entire local history.
The VPS matches exact record content and causal times and stores `copy_receipts`:
`fingerprint`, first `copied_at` and `delete_after = copied_at + 48 hours`.
The server clock sets these UTC times. Repeated copies keep the first deadline.
Changed or newly collected observations are not acknowledged by an old manifest.
A failed or corrupt download sends no acknowledgement. SSH acknowledgement
failure leaves local history saved and remote observations unqueued for that
attempt; retrying is safe. This is a recoverable two-step protocol, not a
cross-machine transaction. The required copy is the SQLite archive; gzip is an
optional derivative, so gzip failure does not undo its receipt.

Once `copy_receipts` exists, `Archive.prune` ignores rolling age deletion and only
removes acknowledged observations whose 48-hour deadline has arrived. Uncopied
observations are retained regardless of age. Cleanup runs at the collector's
next prune cycle, so actual deletion can occur one polling interval after the
deadline; a stopped collector cannot guarantee a wall-clock deletion time.
Shared payloads survive while referenced. Receipt tombstones remain for retry
idempotency. This policy persists across collector restarts and backup restores;
don't deploy an older collector/storage version that ignores receipts.

Manual VPS cleanup (the collector already does this automatically):

```bash
uv run --locked --project storage python -m market_storage cleanup \
  --archive server_bot/tunnel_inputs.sqlite
```

Copied-only retention deliberately grows if downloads stop. Monitor disk space
and copy regularly; never silently discard uncopied data to satisfy a size cap.
The first switch cannot restore records already pruned by the old 24-hour policy.
