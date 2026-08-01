# Implementation Plan: Approval and Safety Boundaries

## Objective

Make Hermes Core safe enough for real autonomous use by introducing explicit approval and safety boundaries for high-impact operations. This is especially important for destructive or irreversible actions such as shell commands, file deletion, git mutation, or network actions.

## Why this matters

The current runtime already has a basic approval flow in [engine/runtime.py](../engine/runtime.py) and [engine/runtime_service.py](../engine/runtime_service.py), but it needs to become more deliberate and policy-driven. The goal is not just to stop bad actions; it is also to make the system trustworthy enough that it can work unattended in a controlled way.

## Design goals

1. High-impact actions require explicit policy control.
2. Safety checks are enforced before execution, not after the fact.
3. The approval path is observable and replayable.
4. The runtime remains deterministic under approval and denial events.

## Proposed architecture

### 1. Risk classification

Every tool should be classified by risk:

- read-only
- write-safe
- destructive
- privileged
- external-network

The classification should be part of the tool contract and used by the runtime before the tool executes.

### 2. Approval policy engine

Add a policy engine that decides whether a given action requires:

- no approval
- human approval
- policy approval
- explicit confirmation for risky operations

The current approval logic in [engine/runtime.py](../engine/runtime.py) should be extended to support policy-driven decisions rather than only a simple workflow transition.

### 3. Action sandboxing

Use the existing tool runtime boundaries in [engine/tool_runtime.py](../engine/tool_runtime.py) as the enforcement point:

- allowed roots for filesystem operations
- allowed commands for shell operations
- allowed git actions
- restricted environment variables or cwd

### 4. Approval event model

Approval should be represented as a first-class runtime event with:

- action type
- actor
- reason
- requested scope
- decision outcome

This keeps approval visible in the run history and makes it recoverable after restart.

## Implementation phases

### Phase 1 — Introduce risk metadata

- add risk classification to tool definitions
- expose this in tool execution requests
- use it in the runtime service to decide whether an action is allowed to proceed

Acceptance criteria:

- every tool invocation has an explicit risk classification
- the runtime can determine whether an operation is low-risk or high-risk

### Phase 2 — Add approval gating for high-risk actions

- require approval for destructive or privileged actions
- preserve the pending approval state in the run record
- support approve/deny transitions through [engine/api.py](../engine/api.py) and [engine/http_adapter.py](../engine/http_adapter.py)

Acceptance criteria:

- dangerous actions pause the workflow until approval is resolved
- the approval state is visible through run queries and events

### Phase 3 — Strengthen guardrails in the runtime

- prevent shell commands that are likely to mutate the filesystem unless explicitly allowed
- enforce root restrictions for file tools
- reject actions that exceed the configured policy scope

Acceptance criteria:

- policy violations fail fast before side effects occur
- the runtime produces useful error output for blocked actions

### Phase 4 — Add approval reason and audit details

- record the reason for approval requests
- persist the final decision in the run history
- surface audit information in run events and artifact metadata

Acceptance criteria:

- every approval decision is attributable and replayable
- audit logs are sufficient to understand why a tool was allowed or blocked

## Files to change

- [engine/models.py](../engine/models.py)
- [engine/runtime.py](../engine/runtime.py)
- [engine/runtime_service.py](../engine/runtime_service.py)
- [engine/tool_runtime.py](../engine/tool_runtime.py)
- [engine/api.py](../engine/api.py)
- [engine/http_adapter.py](../engine/http_adapter.py)

## Tests

Add tests for:

- read-only actions proceed without approval
- destructive actions pause for approval
- denied actions do not mutate state
- approval state survives restart recovery
- guardrails block policy-violating commands

## Estimated effort

- 1 to 3 weeks for a solid initial version
- 3 to 5 weeks for a mature policy system

## Definition of done

This work is complete when high-impact actions are explicitly governed by policy, approval, and auditability, and when the runtime can safely pause or reject hazardous steps without losing track of state.
