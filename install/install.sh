#!/bin/sh
# Bootstrap uv and the locked application before installing user services.
set -eu

install_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir="$install_dir/../server_bot"

if command -v uv >/dev/null 2>&1; then
    uv_bin=$(command -v uv)
elif [ -x "$HOME/.local/bin/uv" ]; then
    uv_bin="$HOME/.local/bin/uv"
else
    if ! command -v curl >/dev/null 2>&1; then
        echo 'curl is required: sudo apt-get install -y ca-certificates curl' >&2
        exit 1
    fi
    installer=$(mktemp)
    trap 'rm -f "$installer"' EXIT HUP INT TERM
    # Download first so a failed request cannot be hidden by a successful sh.
    curl --proto '=https' --tlsv1.2 -LsSf \
        https://astral.sh/uv/0.13.7/install.sh -o "$installer"
    UV_UNMANAGED_INSTALL="$HOME/.local/bin" sh "$installer"
    uv_bin="$HOME/.local/bin/uv"
    "$uv_bin" --version
    rm -f "$installer"
    trap - EXIT HUP INT TERM
fi

"$uv_bin" sync --locked --project "$project_dir"
exec "$uv_bin" run --locked --project "$project_dir" python \
    "$install_dir/bot_services.py" --uv "$uv_bin" install "$@"
