#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "Virtual environment not found at $VENV_DIR" >&2
  exit 1
fi

PYTHON_BIN="$VENV_DIR/bin/python"

if ! "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import importlib.util
raise SystemExit(0 if importlib.util.find_spec('PyInstaller') else 1)
PY
then
  "$PYTHON_BIN" -m pip install PyInstaller
fi

OUTPUT_DIR="$ROOT_DIR/dist"
rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"

"$PYTHON_BIN" -m PyInstaller \
  --clean \
  --name hermes \
  --onefile \
  --add-data "$ROOT_DIR/examples:examples" \
  --distpath "$OUTPUT_DIR" \
  --workpath "$ROOT_DIR/build" \
  --specpath "$ROOT_DIR/build" \
  hermes_cli.py

"$PYTHON_BIN" -m PyInstaller \
  --clean \
  --name hermes-webui-launcher \
  --onefile \
  --distpath "$OUTPUT_DIR" \
  --workpath "$ROOT_DIR/build" \
  --specpath "$ROOT_DIR/build" \
  --add-data "${ROOT_DIR}/desktop_launcher.py:." \
  launcher_entry.py

mkdir -p "$OUTPUT_DIR/examples"
cp -R "$ROOT_DIR/examples/." "$OUTPUT_DIR/examples/"

cat > "$OUTPUT_DIR/release-manifest.json" <<EOF
{
  "name": "Hermes Core",
  "version": "0.1.0",
  "binary": "hermes",
  "examples_dir": "examples"
}
EOF

echo "Built executable at $OUTPUT_DIR/hermes"
echo "Built desktop launcher at $OUTPUT_DIR/hermes-webui-launcher"
echo "Bundled examples at $OUTPUT_DIR/examples"
echo "Release manifest at $OUTPUT_DIR/release-manifest.json"
