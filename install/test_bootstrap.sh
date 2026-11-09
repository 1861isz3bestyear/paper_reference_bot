#!/bin/sh
# Deterministic bootstrap tests without network or a systemd manager.
set -eu
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/bootstrap test.XXXXXX")
trap 'rm -rf "$work"' EXIT HUP INT TERM
mkdir -p "$work/deployment/install" "$work/deployment/server_bot" "$work/bin" "$work/home"
cp "$source_dir/install.sh" "$work/deployment/install/"
for tool in dirname mktemp rm sh chmod cat ln; do
    ln -s "$(command -v "$tool")" "$work/bin/$tool"
done
export HOME="$work/home" PATH="$work/bin" BOOTSTRAP_LOG="$work/commands"
export FAKE_UV="$work/fake-uv"
cat > "$FAKE_UV" <<'UV'
#!/bin/sh
printf '%s\n' '---' >> "$BOOTSTRAP_LOG"
printf '%s\n' "$@" >> "$BOOTSTRAP_LOG"
if [ "${FAIL_SYNC:-0}" = 1 ] && [ "$1" = sync ]; then exit 19; fi
UV
chmod +x "$FAKE_UV"
# Existing uv in PATH: preserve argument boundaries, including spaces.
ln -s "$FAKE_UV" "$work/bin/uv"
sh "$work/deployment/install/install.sh" --entity data-saver --no-start
expected=$(printf '%s\n' '---' sync --locked --project "$work/deployment/install/../server_bot" '---' run --locked --project "$work/deployment/install/../server_bot" python "$work/deployment/install/bot_services.py" --uv "$work/bin/uv" install --entity data-saver --no-start)
[ "$(/bin/cat "$BOOTSTRAP_LOG")" = "$expected" ]
# Failed sync must stop before invoking the service installer.
: > "$BOOTSTRAP_LOG"
if FAIL_SYNC=1 sh "$work/deployment/install/install.sh"; then exit 1; else [ "$?" = 19 ]; fi
[ "$(/usr/bin/wc -l < "$BOOTSTRAP_LOG" | /usr/bin/tr -d ' ')" = 5 ]
rm "$work/bin/uv"
# Missing curl fails with an actionable message.
if sh "$work/deployment/install/install.sh" 2> "$work/error"; then exit 1; fi
/bin/grep -q 'curl is required' "$work/error"
cat > "$work/bin/curl" <<'CURL'
#!/bin/sh
if [ "${FAIL_DOWNLOAD:-0}" = 1 ]; then exit 22; fi
while [ "$1" != -o ]; do shift; done
shift
printf 'exec /bin/cp "$FAKE_UV" "$UV_UNMANAGED_INSTALL/uv"\n' > "$1"
CURL
chmod +x "$work/bin/curl"
# A failed download must never execute the installer or service commands.
: > "$BOOTSTRAP_LOG"
if FAIL_DOWNLOAD=1 sh "$work/deployment/install/install.sh"; then exit 1; else [ "$?" = 22 ]; fi
[ ! -s "$BOOTSTRAP_LOG" ]
[ ! -e "$HOME/.local/bin/uv" ]
# Fresh install, then reuse a local uv absent from PATH without downloading.
/bin/mkdir -p "$HOME/.local/bin"
sh "$work/deployment/install/install.sh" --entity data-saver
/bin/grep -q -- '--version' "$BOOTSTRAP_LOG"
: > "$BOOTSTRAP_LOG"
FAIL_DOWNLOAD=1 sh "$work/deployment/install/install.sh" --entity data-saver
/bin/grep -q "$HOME/.local/bin/uv" "$BOOTSTRAP_LOG"
printf 'Bootstrap tests passed\n'
