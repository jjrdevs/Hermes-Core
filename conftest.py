"""
Pytest root conftest for HERMES Core.

This file is loaded by pytest *before* any test module is collected, so it is
the correct place to pin environment-sensitive defaults that are read at
module-import time.

Why this file exists
--------------------
On 2026-09-10 the production default for HERMES Core was flipped from the
offline ``stub`` adapter to the local Ollama fast-variant (``qwen38-fast-128k``
on ``localhost:11434``). ``DEFAULT_PROVIDER`` / ``DEFAULT_MODEL`` in
``workers.model_adapter`` read ``HERMES_CORE_DEFAULT_PROVIDER`` /
``HERMES_CORE_DEFAULT_MODEL`` at import time.  Without this conftest the test
suite would start hitting the real Ollama endpoint and would fail when it was
not running — a test-suite that should be offline suddenly depends on
infrastructure.

What this does
--------------
Pins the two env vars so that every test process sees:

  HERMES_CORE_DEFAULT_PROVIDER = "stub"        (offline stub adapter)
  HERMES_CORE_DEFAULT_MODEL    = "qwen38-fast-128k"  (model name only)

This is an env-var pin, NOT a code override — production code paths that are
reached after the test process exits are not affected, and any test that
explicitly passes ``provider="ollama"`` still exercises the Ollama adapter
path (it just never actually sends a network request in the unit tests).

To override in a single test, use ``monkeypatch.setenv`` /
``monkeypatch.delenv`` — this conftest only sets the initial process
environment.
"""

import os

# Pin BEFORE any worker/engine module is imported.  The model name is
# recorded here even in stub mode because tests may assert on it; it is never
# used to reach a network endpoint (stub adapter is in-process).
os.environ.setdefault("HERMES_CORE_DEFAULT_PROVIDER", "stub")
os.environ.setdefault("HERMES_CORE_DEFAULT_MODEL", "qwen38-fast-128k")

# Guard: if a test or subprocess explicitly set a different provider before
# collection, respect it (allow CI to override with HERMES_CORE_TEST_PROVIDER).
_override = os.environ.get("HERMES_CORE_TEST_PROVIDER")
if _override:
    os.environ["HERMES_CORE_DEFAULT_PROVIDER"] = _override
