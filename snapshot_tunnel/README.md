# Snapshot tunnel calculations

This package, owned by `paper`, is the canonical implementation used by both
GUI Calculator and the Bybit demo executor. It contains validated option chains,
GEX plans, positive states 1/2/3, negative GEX direction, independently recorded
oscillator votes and receipt-time/freshness checks. Compatibility modules in the
applications re-export the same domain objects rather than maintaining copies.

Deploy this directory alongside `server_bot/`, `storage/`, and `install/`.
`uv sync --locked --project server_bot` installs it through the local dependency.
No backtest checkout, Streamlit server or reference trading process is needed
on the VPS.

The calculator's full test suite measures this package in addition to its
previous production scope, retaining the 92% combined line/branch coverage gate.
The server measures migrated shared modules alongside its previous scope and
new demo executor. Source migrations do not remove coverage obligations.
