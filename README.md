# Hermes Core

**Hermes Core is a deterministic orchestration platform for unattended AI workflows.** It plans, executes, verifies, and recovers multi-step local-AI work overnight or across restarts — with human approval as a first-class control point, and with AI models treated as replaceable capabilities behind stable interfaces.

> Design, architecture, and the decisions below were mine; implementation was done with an AI assistant.

## Why I built it

Long AI-assisted workflows that run unattended fail in predictable ways: they crash mid-run and lose their state, they keep going when they should stop and ask a person, and they hard-depend on one model provider. Hermes Core exists to make those failure modes structural rather than accidental:

- **Survive a crash** — every state change is an event; a restart replays the log and resumes from the last checkpoint.
- **Pause for a human** — approval is a workflow state (`WAITING_APPROVAL`), not a prompt in a terminal.
- **Swap the model** — providers sit behind an adapter interface; the control plane never knows or cares which one is answering.
- **Run offline** — the engine needs only the Python standard library and pytest to run and test; a local model (e.g. Ollama `qwen38-fast-128k`) or the offline `stub` provider is the default.

## Design decisions

The pieces worth defending in a conversation:

1. **Durable per-step state machine with resume.** Each run keeps an `execution_loop` (iteration / max_iterations / phase / stop_reason) and checkpoints after pass; resume replays to the checkpoint and continues. See `engine/runtime_service.py` (`run_task`, `_save_checkpoint`, `execution_loop` state) and `engine/test_execution_loop.py`.
2. **Human-in-the-loop as an architectural primitive.** Approval gates are persisted workflow states driven by `approve <execution_id> --approved-by ...`, with the approver and reason recorded in the event log — auditable, replayable, and impossible to lose to a crash. See the kernel/approval path in `engine/`, `hermes_cli.py` (`command_approve`), and `examples/approval_example.json`.
3. **SQLite-lease-backed background scheduling.** Jobs are rows in SQLite; workers claim them via lease locks so no job is ever double-executed, even with multiple workers or a restarted host. See `engine/scheduler.py` / `engine/spec_scheduler.py`.
4. **Model adapters behind a stable interface.** `workers/model_adapter.py` (`ModelAdapterRouter` / `ModelAdapterFactory`) routes work to the configured provider — Ollama's local model by default, or the `stub` adapter for offline tests. The scheduler, policy engine, and kernel consume the adapter contract, never a provider SDK — which is why the whole suite and default path run on a local model with zero API keys.

## Continuous integration

- **Suite:** `python -m pytest` — 356 tests, ~35s (offline; `conftest.py` pins the `stub` provider by default, so CI needs no token, network, or Ollama).
- **Dependency footprint:** the engine and tests import only the standard library plus `pytest`.
- **Workflow:** `workflows/ci.yml` — matrix over Python 3.10–3.13.
  - Note: the canonical path is `.github/workflows/ci.yml` (kept locally in this repo's working copy / git history). The current maintainer token lacks GitHub's `workflow` scope, so the pushed copy is staged at `workflows/`. To activate: re-auth `gh` with `workflow` scope, then `git mv workflows/ci.yml .github/workflows/ci.yml && git push`. A live Actions badge will be added to this README once the flow above is complete.

## Quick Start

1. Run a workflow:

```bash
python3 hermes_cli.py run examples/design_review_workflow.json --data-dir /tmp/hermes_data
```

2. Check workflow status:

```bash
python3 hermes_cli.py status <workflow_execution_id> --data-dir /tmp/hermes_data
```

3. Approve a waiting workflow:

```bash
python3 hermes_cli.py approve <workflow_execution_id> --data-dir /tmp/hermes_data --approved-by qa-lead --comment "Ready to continue"
```

4. Resume execution after approval or restart:

```bash
python3 hermes_cli.py resume <workflow_execution_id> --data-dir /tmp/hermes_data
```

Preview a workflow without executing its steps:

```bash
python3 hermes_cli.py run examples/design_review_workflow.json --dry-run --data-dir /tmp/hermes_data
```

Rollback an applied task edit from a checkpoint:

```bash
python3 hermes_cli.py rollback <checkpoint_id> --data-dir /tmp/hermes_data
```

5. List persisted runtime assets:

```bash
python3 hermes_cli.py list workflows --data-dir /tmp/hermes_data
python3 hermes_cli.py list executions --data-dir /tmp/hermes_data
python3 hermes_cli.py list tools --data-dir /tmp/hermes_data
```

## Architecture Overview

Hermes Core is built around an event-sourced runtime kernel. The runtime persists:

- workflow definitions
- event logs
- artifacts
- tool metadata

On startup, Hermes replays persisted events to recover workflow state and resume execution from the last known point.

## Example Workflows

Example workflows live in `examples/` and include:

- `hello_world.json`
- `two_step_pipeline.json`
- `approval_example.json`
- `tool_example.json`
- `design_review_workflow.json` (demonstration workflow)

## CLI Reference

Supported commands:

- `validate <workflow.json>`: Validate workflow JSON structure.
- `run <workflow.json>`: Start a new persisted workflow execution.
- `run <workflow.json> --dry-run`: Preview planned workflow steps without executing them.
- `resume <workflow_execution_id>`: Resume a persisted execution.
- `approve <workflow_execution_id>`: Approve a workflow paused for approval.
- `rollback <checkpoint_id>`: Restore the file state recorded before a checkpointed task edit.
- `status <workflow_execution_id>`: Show human-readable workflow status.
- `artifacts <workflow_execution_id>`: Show produced artifacts.
- `list workflows`: List persisted workflow definitions.
- `list executions`: List persisted workflow executions.
- `list tools`: List persisted tools.

Global option:

- `--data-dir`: Directory for Hermes runtime persistence (default is `~/.hermes/data`, or the `HERMES_DATA_DIR` override).

## Testing

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest          # 356 tests, offline, no external services
```

## Packaging and Smoke Testing

Build the packaged executable with:

```bash
./build.sh
```

Run the end-to-end smoke test against the built binary:

```bash
./scripts/smoke_test.sh
```

The smoke test validates that the packaged executable can validate a sample workflow, run it, create persisted state, inspect that state, and fail clearly for an invalid workflow input.

## Recovery Model

Hermes recovers runtime state by replaying the event log stored in SQLite. Every state change is captured as an event. This includes workflow creation, step execution lifecycle events, artifact creation, tool usage, approval request emission, and approval grant events.

## Event Model

Hermes persists a set of core event types:

- `WORKFLOW_CREATED`
- `STEP_EXECUTION_CREATED`
- `STEP_EXECUTION_STARTED`
- `STEP_EXECUTION_COMPLETED`
- `ARTIFACT_CREATED`
- `TOOL_REQUESTED`
- `TOOL_INVOKED`
- `APPROVAL_REQUIRED`
- `APPROVAL_GRANTED`
- `WORKFLOW_COMPLETED`
- `WORKFLOW_FAILED`

Each event is replayed on startup to reconstruct the exact workflow state.

## Demonstration Workflow

The `examples/design_review_workflow.json` workflow shows a realistic multi-step run with:

1. architecture planning by an `architect`
2. a required approval step for a `human_operator`
3. a follow-on implementation step by a `developer`
4. a tool request during planning via the `filesystem` tool
5. artifact creation for architecture and implementation outputs
6. recovery after restart using persisted state

---

**Portfolio:** [portfolio.jjrdev.com/hermes-core/case-study](https://portfolio.jjrdev.com/hermes-core/case-study) — the design story, architecture diagrams, and process notes for this project.
