# Execution Contract

## Purpose

Defines the runtime execution unit Hermes Core controls.

## Execution shape

An execution represents a single runtime invocation of a planned capability within a workflow.

Example execution schema:

```json
{
  "execution_id": "uuid",
  "workflow_id": "uuid",
  "workflow_definition_id": "workflow-abc",
  "step_id": "developer-1",
  "capability_required": "developer",
  "status": "PENDING",
  "worker_assigned": {
    "worker_type": "developer",
    "worker_id": "developer-worker-01"
  },
  "model_assigned": {
    "adapter": "ollama",
    "model_name": "qwen3-coder-30b"
  },
  "input_artifacts": ["architecture_8f92b31"],
  "output_artifacts": ["implementation_patch_12ab34"],
  "created_at": "2026-07-07T12:00:00Z",
  "started_at": null,
  "completed_at": null,
  "events": ["event_1", "event_2"],
  "policy_context": {
    "approved": true,
    "policy_ids": ["policy-001"]
  }
}
```

## Execution states

- `PENDING`
- `RESOLVING`
- `ASSIGNED`
- `RUNNING`
- `VALIDATING`
- `COMPLETED`
- `FAILED`
- `BLOCKED`
- `AWAITING_INPUT`

## Fields

- `execution_id` — unique runtime invocation id
- `workflow_id` — parent workflow execution id
- `workflow_definition_id` — source workflow definition id
- `step_id` — planned execution template id
- `capability_required` — capability the execution needs
- `status` — current execution state
- `worker_assigned` — worker chosen to run the execution
- `model_assigned` — model adapter chosen for execution
- `input_artifacts` — artifacts used as inputs
- `output_artifacts` — artifacts produced by this execution
- `created_at` — execution creation timestamp
- `started_at` — execution start timestamp
- `completed_at` — execution completion timestamp
- `events` — related event ids
- `policy_context` — policy decisions and approvals that authorized execution

## Execution lifecycle

Hermes runtime loop for an execution:

1. `PENDING` — execution is created.
2. `RESOLVING` — Hermes resolves capability, worker, and model.
3. `ASSIGNED` — worker and model are assigned.
4. `RUNNING` — execution is in progress.
5. `VALIDATING` — outputs are validated against policy and acceptance criteria.
6. `COMPLETED` — execution succeeded.
7. `FAILED` — execution failed or was blocked.

## Rules

- Executions are the unit of control in Hermes.
- Workers and models are replaceable implementation details.
- Executions must be auditable through events, artifacts, and policy context.
- Hermes owns execution state and transitions.
