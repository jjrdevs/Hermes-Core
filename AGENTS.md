# Hermes Core — Agent Context

## Quick Start
- Activate env: `source .venv/bin/activate`
- Health check: `python -m pytest engine/test_runtime.py --tb=short -q` (expect 42 passed)
- Build bundle: `./build.sh` → `dist/hello` (verify with `file dist/hello`)

## Conventions & Rules
- **Test first.** Fix bugs by following test failures only — do not guess. Always run pytest --tb=short -q AFTER changing code to verify green status.
- New capabilities go under `capabilities/<name>/` — follow existing pattern (capability.json + implementation.py)
- Source layout reference:
  - `engine/` → scheduler/runtime entrypoint
  - `workers/local_worker.py` → model execution worker (single-shot, no delegation yet)
    - **CRITICAL**: Adapter only hits `/api/generate`, not `/api/chat`. Function calling not supported in current version. Rewrite adapter from scratch targeting `/api/chat` + function_call support to unlock full tool-calling capabilities for all registered Ollama models.

## What goes here vs what goes through me
hermes-core is a robust workflow/runtime — use it when:
- **Building real features that need the engine** (workflow steps, schedulers, workers, adapters)
- Debugging or refactoring internal mechanics? I'll delegate to hermes-core workdir so you have full project context.

## Source Map
See `references/` in this codebase for architecture details and known gaps.

--- END OF GUIDE ---
