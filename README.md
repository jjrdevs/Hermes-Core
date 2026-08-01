# Hermes Core

Hermes Core is a deterministic orchestration platform for local AI workflows.

The system separates workflow control, state management, policy enforcement, and artifact lineage from model execution. AI models are treated as replaceable capabilities behind stable interfaces.

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
