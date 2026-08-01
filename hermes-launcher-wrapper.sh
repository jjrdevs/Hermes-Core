#!/usr/bin/env bash
set -euo pipefail

# Wrapper to be called directly by the desktop Exec line. Uses absolute paths
# so desktop environments can execute it without shell quoting issues.

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$HOME/.local/share/hermes"
mkdir -p "$LOG_DIR"

# Default command (packaged hermes run example)
CMD_BIN="$ROOT_DIR/dist/hermes"
DEFAULT_CMD="$CMD_BIN run $ROOT_DIR/examples/hello_world.json --data-dir $HOME/.local/share/hermes/data"

HERMES_CORE_CMD="${HERMES_CORE_CMD:-$DEFAULT_CMD}"
export HERMES_CORE_CMD

# Default WebUI location to try if the launcher is executed from this repo.
DEFAULT_WEBUI_ROOT="$HOME/Applications/hermes-webui"
if [[ -d "$ROOT_DIR/../Applications/hermes-webui" ]]; then
  DEFAULT_WEBUI_ROOT="$ROOT_DIR/../Applications/hermes-webui"
fi
HERMES_WEBUI_ROOT="${HERMES_WEBUI_ROOT:-$DEFAULT_WEBUI_ROOT}"
export HERMES_WEBUI_ROOT

# Prefer the real Hermes WebUI on 8787; use the local placeholder only as a fallback.
HERMES_WEBUI_URL="${HERMES_WEBUI_URL:-http://127.0.0.1:8787}"
export HERMES_WEBUI_URL
HERMES_WEBUI_PYTHON="${HERMES_WEBUI_PYTHON:-$HOME/.hermes/hermes-agent/venv/bin/python}"
export HERMES_WEBUI_PYTHON

echo "[wrapper] Starting launcher at $(date --iso-8601=seconds)" >> "$LOG_DIR/desktop-launcher.log"
echo "[wrapper] HERMES_CORE_CMD=$HERMES_CORE_CMD" >> "$LOG_DIR/desktop-launcher.log"
echo "[wrapper] HERMES_WEBUI_ROOT=$HERMES_WEBUI_ROOT" >> "$LOG_DIR/desktop-launcher.log"

echo "[wrapper] HERMES_WEBUI_URL=$HERMES_WEBUI_URL" >> "$LOG_DIR/desktop-launcher.log"

"$ROOT_DIR/hermes-launcher.sh" "$@" >> "$LOG_DIR/desktop-launcher.log" 2>&1 &

exit 0
