#!/usr/bin/env bash
set -euo pipefail

# Simple Tk-free launcher: starts Hermes Core, tries to boot the real Hermes
# WebUI, and opens that URL when it becomes available. A small local fallback
# page is used only if the real WebUI cannot be launched.

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$HOME/.local/share/hermes"
mkdir -p "$LOG_DIR"

WEBUI_URL="${HERMES_WEBUI_URL:-http://127.0.0.1:8787}"
LOCAL_WEBUI_PORT="${HERMES_LOCAL_WEBUI_PORT:-8765}"
LOCAL_WEBUI_SCRIPT="$ROOT_DIR/local_webui.py"
DEFAULT_WEBUI_ROOT="$HOME/Applications/hermes-webui"
if [[ -d "$ROOT_DIR/../Applications/hermes-webui" ]]; then
  DEFAULT_WEBUI_ROOT="$ROOT_DIR/../Applications/hermes-webui"
fi
WEBUI_ROOT="${HERMES_WEBUI_ROOT:-$DEFAULT_WEBUI_ROOT}"
WEBUI_START_SCRIPT="$WEBUI_ROOT/start.sh"
AGENT_PYTHON="${HERMES_WEBUI_PYTHON:-$HOME/.hermes/hermes-agent/venv/bin/python}"
if [[ ! -x "$AGENT_PYTHON" ]]; then
  AGENT_PYTHON=""
fi

log() {
  echo "$*" >> "$LOG_DIR/desktop-launcher.log"
}

find_hermes_binary() {
  # Prefer packaged binary
  if [[ -x "$ROOT_DIR/dist/hermes" ]]; then
    echo "$ROOT_DIR/dist/hermes"
    return 0
  fi
  if command -v hermes >/dev/null 2>&1; then
    command -v hermes
    return 0
  fi
  return 1
}

start_hermes_core() {
  # Use an explicit command if provided via HERMES_CORE_CMD. This allows the
  # launcher to start Hermes with the right subcommand (for example:
  # "hermes run examples/hello_world.json") instead of invoking the CLI with
  # no args which just prints usage and exits.
  if [[ -z "${HERMES_CORE_CMD:-}" ]]; then
    log "HERMES_CORE_CMD not set; skipping Hermes Core startup."
    return 1
  fi

  nohup bash -c "$HERMES_CORE_CMD" >> "$LOG_DIR/hermes.log" 2>&1 &
  disown || true
  log "Started Hermes Core with: $HERMES_CORE_CMD"
}

start_placeholder_server() {
  if [[ -x "$LOCAL_WEBUI_SCRIPT" ]] || [[ -f "$LOCAL_WEBUI_SCRIPT" ]]; then
    if ! (echo > /dev/tcp/127.0.0.1/$LOCAL_WEBUI_PORT) >/dev/null 2>&1; then
      nohup python3 "$LOCAL_WEBUI_SCRIPT" "$LOCAL_WEBUI_PORT" "$WEBUI_URL" >> "$LOG_DIR/local-webui.log" 2>&1 &
      disown || true
      sleep 0.2
    fi
  fi
}

is_http_ready() {
  local url="$1"
  python3 - "$url" <<'PY' >/dev/null 2>&1
import sys
import urllib.request
url = sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=1) as response:
        raise SystemExit(0 if response.status < 500 else 1)
except Exception:
    raise SystemExit(1)
PY
}

start_real_webui() {
  if [[ -f "$WEBUI_START_SCRIPT" ]]; then
    log "Attempting Hermes WebUI startup from $WEBUI_ROOT"
    if is_http_ready "$WEBUI_URL"; then
      log "Hermes WebUI is already available at $WEBUI_URL"
      return 0
    fi

    if [[ -n "$AGENT_PYTHON" ]]; then
      export HERMES_WEBUI_PYTHON="$AGENT_PYTHON"
      log "Exported HERMES_WEBUI_PYTHON=$HERMES_WEBUI_PYTHON"
    fi
    nohup bash "$WEBUI_START_SCRIPT" >> "$LOG_DIR/webui.log" 2>&1 &
    disown || true

    for attempt in $(seq 1 60); do
      if is_http_ready "$WEBUI_URL"; then
        log "Hermes WebUI became available at $WEBUI_URL"
        return 0
      fi
      sleep 0.5
    done

    log "Hermes WebUI did not become ready at $WEBUI_URL"
    return 1
  fi

  log "Hermes WebUI start script not found at $WEBUI_START_SCRIPT"
  return 1
}

open_url() {
  local target="$1"
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$target" >/dev/null 2>&1 || true
    return 0
  fi
  if command -v gio >/dev/null 2>&1; then
    gio open "$target" >/dev/null 2>&1 || true
    return 0
  fi
  echo "Open your browser and go to: $target"
}

case "${1:-}" in
  --no-start)
    start_placeholder_server
    LOCAL_PLACEHOLDER_URL="http://127.0.0.1:$LOCAL_WEBUI_PORT"
    open_url "$LOCAL_PLACEHOLDER_URL"
    exit 0
    ;;
  --dry-run)
    if [[ -n "${HERMES_CORE_CMD:-}" ]]; then
      echo "Would start Hermes Core with: $HERMES_CORE_CMD and try to open $WEBUI_URL"
    else
      echo "HERMES_CORE_CMD not set; would try to open $WEBUI_URL"
    fi
    exit 0
    ;;
esac

if start_hermes_core; then
  sleep 0.4
fi

if start_real_webui; then
  open_url "$WEBUI_URL"
  exit 0
fi

start_placeholder_server
LOCAL_PLACEHOLDER_URL="http://127.0.0.1:$LOCAL_WEBUI_PORT"
log "Falling back to local placeholder page at $LOCAL_PLACEHOLDER_URL"
open_url "$LOCAL_PLACEHOLDER_URL"

exit 0
