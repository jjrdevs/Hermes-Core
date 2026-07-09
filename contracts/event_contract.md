# Event Contract

## Purpose

Defines the event model Hermes Core uses as the source of truth for operational execution.

## Event shape

Each event records a meaningful state change or decision in the runtime.

Example event schema:

```json
{
  "event_id": "uuid",
  "event_type": "WORKFLOW_CREATED",
  "timestamp": "2026-07-07T12:00:00Z",
  "workflow_id": "uuid",
  "execution_id": null,
  "payload": {
    "workflow_name": "build_oauth",
    "initiated_by": "user"
  }
}
```

## Event types

- `WORKFLOW_CREATED`
- `WORKFLOW_UPDATED`
- `STEP_SCHEDULED`
- `WORKER_ASSIGNED`
- `WORKER_STARTED`
- `WORKER_COMPLETED`
- `ARTIFACT_CREATED`
- `ARTIFACT_VALIDATED`
- `APPROVAL_REQUIRED`
- `APPROVAL_GRANTED`
- `POLICY_DENIED`
- `WORKFLOW_COMPLETED`
- `WORKFLOW_FAILED`

## Event rules

- Events are immutable and append-only.
- Hermes reconstructs runtime state from event history.
- Events should be expressive enough to support recovery, audit, and replay.
- Events may include references to artifacts, steps, workers, policies, and approvals.

## Example payloads

`WORKER_ASSIGNED`

```json
{
  "execution_id": "uuid",
  "role": "developer",
  "worker_id": "worker-abc",
  "capabilities_requested": ["code", "reasoning"]
}
```

`ARTIFACT_CREATED`

```json
{
  "artifact_id": "8f92b31",
  "artifact_type": "architecture",
  "version": 3,
  "creator": "architect",
  "source_execution_id": "uuid"
}
```

`POLICY_DENIED`

```json
{
  "action": "modify_production_database",
  "reason": "policy requires human approval"
}
```
