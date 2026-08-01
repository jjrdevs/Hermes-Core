#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="Hermes Launcher"
# Prefer a lightweight shell launcher if present to avoid GUI/Tk dependencies.
if [[ -x "$ROOT_DIR/hermes-launcher.sh" ]]; then
  EXECUTABLE="$ROOT_DIR/hermes-launcher.sh"
else
  EXECUTABLE="$ROOT_DIR/dist/hermes-webui-launcher"
fi
DESKTOP_DIR="$HOME/.local/share/applications"
DESKTOP_FILE="$DESKTOP_DIR/hermes-launcher.desktop"
ICON_PATH="$ROOT_DIR/hermes-launcher.png"

mkdir -p "$DESKTOP_DIR"

[ -d "$HOME/.local/share/hermes" ] || mkdir -p "$HOME/.local/share/hermes"
LAUNCHER_LOG="$HOME/.local/share/hermes/desktop-launcher.log"

# Default Hermes start command when clicking the launcher. Users can override
# by setting HERMES_CORE_CMD before running this installer. The command should
# include the full start invocation for the Hermes CLI (e.g. "hermes run ...").
: "${HERMES_CORE_CMD:-}"
# Prefer the packaged binary in dist/ if present; otherwise fall back to 'hermes' on PATH.
if [[ -x "$ROOT_DIR/dist/hermes" ]]; then
  CMD_BIN="$ROOT_DIR/dist/hermes"
else
  CMD_BIN="hermes"
fi

DEFAULT_CMD="$CMD_BIN run $ROOT_DIR/examples/hello_world.json --data-dir $HOME/.local/share/hermes/data"
LAUNCH_CMD="${HERMES_CORE_CMD:-$DEFAULT_CMD}"

# Use a wrapper script with an absolute path as Exec to avoid quoting issues
# in desktop environments.
WRAPPER="$ROOT_DIR/hermes-launcher-wrapper.sh"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
Comment=Launch Hermes WebUI and Hermes Core
Exec=$WRAPPER
Icon=$ICON_PATH
Terminal=false
Categories=Utility;Development;
StartupNotify=true
EOF

chmod +x "$DESKTOP_FILE"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
fi

echo "Installed desktop entry at $DESKTOP_FILE"
echo "You may need to log out/in before it appears in your app menu."
