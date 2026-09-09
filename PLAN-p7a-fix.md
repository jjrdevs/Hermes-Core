# p7a fix plan — make the pipeline actually run a tool loop against a real model

Date: 2026-09-05
Status: PROPOSED (not yet implemented)
Context: p0–p3 done; p6 ~90%; p7a (stub→no-op) proven negative; p7 (E2E) blocked.

## 1. Root-cause map (confirmed by reading the code, not assumed)

Live error currently observed when starting a run with `workflows/coding.json`:
```
Immutable tool conflict for id filesystem
```

### Bug 1 (in MY coding.json — first blocker, easiest)
`workflows/coding.json` declares a `tools` array that re-registers `filesystem`
(tool_id = "filesystem", actions=[read, read_file, list_directory, search_text,
search_files, write, write_file, create_file]). The kernel already treats
`filesystem` as a built-in that the service itself wires via
`FilesystemTool(allowed_roots=...)` — see `engine/runtime_service.py:899` where
the service constructs a real FilesystemTool for the policy/approval path, and
`engine/runtime.py:123-125` where `ToolRegistry`/`SQLiteToolStore` are seeded by
the kernel's `_load_tools()` at boot. So `register_tool` at
`engine/runtime.py:334` sees an existing id and refuses to override it.

The kernel's `assign_execution` + `request_tool` path (already implemented and
tested in `test_policy.py`, `test_tool_guardrails.py`) is what enforces
`allowed_tools` from `step.constraints` against this pre-seeded registry. That
mechanism is correct and should stay.

Fix: my workflow JSON should NOT re-declare `tools`; it should rely on the
pre-seeded registry via `constraints.allowed_tools` on each step (which it
already does). Remove the `tools` array from `workflows/coding.json`.

### Bug 2 (kernel wiring — the actual "no real edits" root cause)
`runtime_service.py:121` builds the worker as:
```python
worker = LocalWorker(model_adapter)
```
i.e. `LocalWorker.__init__` is called WITHOUT `tools=` and WITHOUT
`tool_executor=`, so `self.tools` is `None` and it falls into the legacy
single-shot branch (`local_worker.py:87-107`): one `model.generate(prompt)`
call, no tool loop, no FilesystemTool dispatch. The model's text reply is
recorded as an artifact, and the only side-effect is that each `allowed_tools`
entry produces a `ToolRequest` recommendation the kernel then dispatches via
`self.kernel.request_tool(...)` at line ~133 — but the model never actually
decides to edit a file, it just sees a prompt about files.

Fix: at `runtime_service.py:121`, pass both `tools` (OpenAI-style schemas
derived from the workflow's allowed_tools + FilesystemTool's action metadata)
and a `tool_executor` closure bound to `self.execute_tool_request` (line ~700-746)
with `run_id`/`step_execution_id`. Then the model drives the loop and our
kernel's policy engine (strict sandbox, writable_paths, per-action caps)
enforces every edit before it lands on disk.

### Bug 3 (router + factory — provider routing is hardcoded to ollama+stub)
The webui sends `provider="custom"` (that's the hermes-webui label for the
local Ollama stack — see memory: "models added to the existing Ollama stack").
But `runtime_service.py:60-88` `_build_model_router` only builds two provider
profiles: `stub` (cheap, available) and `ollama` (local, available). It sets
`preferred_provider = provider` (whatever the webui sent — "custom"), and
`ModelAdapterRouter._select_provider` then falls through to
"simple + cheapest available" = `stub`. Then `ModelAdapterFactory.create`
(`model_adapter.py:491-496`) returns a `StubModelAdapter` for everything except
`provider == "ollama"`. So `qwen38-fast-128k` (a provider the webui calls
"custom", backed by Ollama at localhost:11434) ends up in the stub. The
adapter code for the OpenAI-compatible
`/v1/chat/completions` + tool-call loop is already implemented in
`OllamaModelAdapter._chat_completion` and `generate_with_tools` (lines 239-336,
verified to work) — it's just not being routed to for `custom`.

Fix options (pick ONE; both touch the same 3 small spots):
(a) Map the webui's `custom` provider string onto the `ollama` provider in
    `runtime_service._build_model_router` before it builds profiles (e.g.
    `"custom" -> "ollama"`), so the existing ollama profile + adapter is
    reused. Simplest; zero new classes.
(b) Add `custom` as a third `ProviderProfile` in the same list, using the
    same `OllamaModelAdapter` (optionally renamed to `OpenAICompatAdapter`,
    but not required for correctness) with `endpoint` + `api_key` (None for
    local Ollama, present for a real hosted endpoint) pulled from the webui's
    config file at `~/.hermes/config.yaml` (custom_providers section).
    More future-proof; more code to write.

Recommendation: (a) for now (matches user's "standard-stack integration"
preference — we're just pointing the existing adapter at the model the
user already runs locally). (b) is the correct shape once we need a second
non-Ollama OpenAI-compat backend.

### Bug 4 (known, separate, already fixed in code — needs live confirmation)
`api/runtime_adapter.py:75` `RunStatus` dataclass and `routes.py`
adapter `get_run` returning it — last confirmed 500 when webui's
`j(handler, status)` tried JSON-serializing a non-dict object. The patch at
L632 was applied but the service is still pre-restart (PID 368369 from
before the patch), so this is UNVERIFIED. Need a fresh `GET /api/runtime/run/{id}`
against a newly-restarted service with one of the fixes above applied.

### Bug 5 (minor)
`runtime_service.py:62-78` `_route_provider` sets `provider_health` for the
requested provider as `unavailable` when it's not in the profile list. That
diagnostic is noisy and confusing now that we're mapping `custom`->`ollama`.
Not a correctness blocker; leave for a follow-up cleanup pass.

## 2. Fix list (ordered, each one independently verifiable)

1. **Fix my workflow JSON** (Bug 1) — edit
   `workflows/coding.json`: remove the `tools` array; each step already has
   `constraints.allowed_tools=["filesystem"]`. Zero other files touched.
   Verify: `POST /api/pipeline/run` no longer raises
   `Immutable tool conflict`. Expected: either the run starts (and completes
   or pauses at the approval gate) or — if Bug 2/3 are still present — the
   run still does no real edits.
2. **Wire the real tool loop** (Bug 2) — edit
   `engine/runtime_service.py::_execute_step` (line 121) to:
   - Build an OpenAI-style `tools` list from the step's `allowed_tools`
     (filesystem only, with actions `read_file`, `list_directory`,
     `search_text`, `write_file`, `create_file`),
     with schemas describing `action: str` and action params.
   - Wrap `self.execute_tool_request` as a `tool_executor` closure that
     resolves the envelope status (only `success` returns payload; `error`
     re-raises or returns an error string so the model can adapt),
     passing `run_id` and `step_execution_id` for the policy/approval path.
   - Construct `LocalWorker(model_adapter, tools=..., tool_executor=...,
     max_iterations=8)` (max_iterations is already the constructor default,
     but pass it explicitly for clarity).
   Verify: with a real model (Bug 3 fixed), the model should call
   `list_directory` / `read_file` / `write_file` during the `implement`
   step, and we should see `TOOL_EXECUTION` events in the run's `events`
   list, and actual file changes under the test workspace.
3. **Route `custom` provider to the OpenAI-compat adapter** (Bug 3, option
   (a)) — edit `runtime_service.py::_build_model_router` to normalize
   `provider in {"custom", "openai", "openrouter", "local"} -> "ollama"`
   before building `ProviderProfile`s (keep `stub` as the fallback so a
   broken Ollama still doesn't hard-crash, but the ollama profile becomes the
   preferred candidate). Verify: `ModelAdapterFactory.create(config)` is now
   called with `config.provider == "ollama"` for a `custom` provider
   request, and returns an `OllamaModelAdapter` (not a stub). `qwen38-fast-128k`
   hits `http://localhost:11434/v1/chat/completions` — confirm with a one-shot
   call through the adapter (offline smoke, same code path as Step 2).
4. **RunStatus 500 (Bug 4)** — confirm with a live `GET /api/runtime/run/{id}`
   after restart; if still 500, add `.to_dict()` (or equivalent) to the
   adapter's `get_run` so `j(handler, status)` gets a plain dict.
5. **p7a live re-test** — prime→run with a tiny brief and a test file in the
   workspace's scratch dir; expect `implement` to produce at least one
   FilesystemTool `write_file` call against that path within the approved
   window, and `verify` to `read_file` it back. Then p7: approve → continue →
   cancel mid-way on a longer brief; verify status transitions
   WAITING_APPROVAL → RUNNING → CANCELLED show up correctly in
   `get_run`.

## 3. What I will NOT change (guardrails)
- I will NOT touch `workflows/coding.json`'s `steps`/`transitions` structure —
  the 3-step + single approval-transition design is correct, and the p7 E2E
  depends on that exact shape.
- I will NOT re-open the hermes-core `engine/` module for new abstractions
  (no new adapters, no new worker classes, no new kernel methods). All five
  fixes above are local to 3 files:
    * `workflows/coding.json`
    * `engine/runtime_service.py` (2 spots: router + `_execute_step`)
    * `workers/model_adapter.py` (0 spots if option (a) is used for
      Bug 3; only rename if I go with option (b))
    * `api/runtime_adapter.py` in webui (0 spots unless Bug 4 resurfaces
      after restart)
- I will NOT restart the webui myself — that's a sudo action on the user's
  machine. I'll hand the user the restart command after I land all 4 file
  edits + the verification smoke.

## 4. Verification plan (I do these before asking user to restart)
1. `python3 -m compileall` on all touched files — catches any syntax issues.
2. Offline smoke of the adapter routing:
   ```
   python3 -c "
   import sys; sys.path.insert(0,'.')
   from engine.runtime_service import RuntimeService
   rs = RuntimeService(data_dir='/tmp/p7a_smoke')
   adapter = rs._build_model_adapter(provider='custom', model_name='qwen38-fast-128k')
   print(type(adapter).__name__, adapter.config)
   "
   ```
   Expected: `OllamaModelAdapter` (not `StubModelAdapter`) with
   `endpoint=http://localhost:11434`, `model_name=qwen38-fast-128k`.
3. Offline smoke of the LocalWorker tool path using the real Ollama model on
   a tiny task with a sandbox directory — expect the model to call `list_directory`
   or `read_file` at least once before finishing.
4. Then I'll hand the user the systemd restart command + a ready-to-pipe curl
   to fire the p7a/p7 E2E, so we get the live proof on the user's machine
   (which is the only one that actually has the service running).

## 5. Open questions for the user (none are blockers to start Step 1-3)
- Do you want me to go with Bug-3 option (a) (map `custom`->`ollama`,
  ~6 lines) or option (b) (new ProviderProfile that reads
  `custom_providers` from config, ~40 lines)? My call: (a) now, (b) later
  when you actually want to hit a second hosted endpoint without Ollama
  in the loop.
- After the fix, is p7a a full "plan→approve→implement→verify" run with
  a real model writing to a scratch file? Or do you want the p7a smoke
  to be a single-step "implement one file edit" so the proof is tighter
  and cheaper (faster iteration)? I'd lean toward the tighter one until
  we've confirmed the loop works, then run the full p7.
