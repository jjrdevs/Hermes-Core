# ADR-004: Event-Driven State

## Status

Proposed

## Context

Hermes Core must remain recoverable, auditable, and deterministic. Workflow state should be reconstructable from a history of events.

## Decision

- Use an event log as the primary source of truth.
- Derive workflow and execution state from ordered events.
- Persist every meaningful state transition and artifact operation.

## Rationale

- Event sourcing supports recovery after crashes.
- Events provide a natural audit trail for autonomous execution.
- The design separates intent from current state, making replay and debugging easier.

## Event examples

- `WORKFLOW_CREATED`
- `EXECUTION_SCHEDULED`
- `WORKER_INVOCATION_STARTED`
- `WORKER_INVOCATION_COMPLETED`
- `ARTIFACT_CREATED`
- `WORKFLOW_TRANSITIONED`
- `APPROVAL_REQUESTED`
- `APPROVAL_GRANTED`

## Consequences

Positive:

- Hermes can recover in-progress workflows after restart.
- The system can support replay-based diagnostics.
- Audit and lineage become first-class concepts.

Negative:

- More implementation complexity than a simple CRUD state store.
- Event schema must be managed carefully.

## Recovery

- On startup, Hermes reads the event log and rebuilds workflow/execution state.
- Incomplete worker invocations can be marked as `FAILED` or retried according to policy.
- Events should include enough context to avoid ambiguity when replayed.
