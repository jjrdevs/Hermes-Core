#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_SCRIPT="$ROOT_DIR/build.sh"
BINARY="$ROOT_DIR/dist/hermes"
DATA_DIR="$(mktemp -d)"
trap 'rm -rf "$DATA_DIR"' EXIT

if [[ ! -x "$BINARY" ]]; then
  echo "Packaged binary not found; building it now..."
  "$BUILD_SCRIPT"
fi

if [[ ! -x "$BINARY" ]]; then
  echo "Failed to build packaged binary" >&2
  exit 1
fi

VALIDATE_OUTPUT="$($BINARY validate "$ROOT_DIR/examples/hello_world.json" 2>&1)"
echo "$VALIDATE_OUTPUT"

RUN_OUTPUT="$($BINARY run "$ROOT_DIR/examples/hello_world.json" --data-dir "$DATA_DIR" 2>&1)"
echo "$RUN_OUTPUT"

EXECUTION_ID="$(printf '%s
' "$RUN_OUTPUT" | sed -n 's/^Workflow execution: //p' | tail -n 1)"
if [[ -z "$EXECUTION_ID" ]]; then
  echo "Failed to capture workflow execution id from run output" >&2
  exit 1
fi

STATUS_OUTPUT="$($BINARY status "$EXECUTION_ID" --data-dir "$DATA_DIR" 2>&1)"
echo "$STATUS_OUTPUT"

ARTIFACT_OUTPUT="$($BINARY artifacts "$EXECUTION_ID" --data-dir "$DATA_DIR" 2>&1)"
echo "$ARTIFACT_OUTPUT"

if [[ ! -f "$DATA_DIR/events.db" ]]; then
  echo "Expected persisted event database was not created" >&2
  exit 1
fi

if "$BINARY" validate "$ROOT_DIR/examples/does_not_exist.json" >/dev/null 2>&1; then
  echo "Missing workflow input unexpectedly succeeded" >&2
  exit 1
fi

echo "Smoke test passed"
