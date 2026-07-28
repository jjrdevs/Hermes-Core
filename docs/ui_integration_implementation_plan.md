# UI Integration Implementation Plan

## Goal

Implement the missing architecture needed for Hermes WebUI to use Hermes Core as a real runtime bridge for structured, hybrid workflow execution.

## Audit summary

The current repository already has most of the execution substrate needed for this architecture. The main issue is that the existing Hermes Core surface is still effectively a one-shot execution helper, not yet a UI-facing runtime bridge.

### What is already in place

- Hermes Core already has a working execution engine in [engine/runtime.py](engine/runtime.py)
- workflow state is persisted through the event log and artifact store
- policy evaluation and approval gating already exist in the kernel
- basic lifecycle APIs are available in [engine/api.py](engine/api.py)
- the HTTP adapter stub already exists in [engine/http_adapter.py](engine/http_adapter.py)
- the WebUI side already has a runtime adapter seam that expects start/observe/status/control-style operations

### What is missing

The missing work is not a second runtime engine. The missing work is a thin, stateful bridge that preserves run lifecycle across calls.

The current implementation has several concrete gaps:

1. The current runtime service is effectively one-shot
   - [engine/runtime_service.py](engine/runtime_service.py) starts a workflow and immediately advances it to completion or approval pause
   - this makes start/observe/approve flows awkward because the UI cannot reliably poll a run that is not being tracked as a long-lived object

2. The current API surface is not yet a proper UI runtime contract
   - it can start and inspect a workflow, but it does not yet maintain a stable run registry with lifecycle status, active controls, and event cursor state across requests

3. The current adapter contract is too implicit
   - WebUI expects a runtime boundary that can start a run, observe progress, fetch status, and handle approval controls in a consistent way
   - the existing bridge surface does not yet make those semantics explicit or durable

4. The current state model is not sufficient by itself for UI-driven control
   - the kernel can reconstruct state from events, but the UI still needs a stable run record that survives a request boundary and is not tied only to an in-memory service object

## Corrected architecture

The correct architecture is:

- Hermes Core remains the source of truth for workflow execution, event replay, policy enforcement, and artifact persistence
- a thin runtime bridge in Hermes Core manages UI-facing run lifecycle state
- WebUI uses the existing adapter seam to talk to that bridge through a narrow contract

This should not bypass the existing kernel. The bridge should orchestrate the kernel rather than duplicate its logic.

## Key corrections to the plan

The plan should not assume that a new “workflow runner” is needed. The current kernel already covers execution. The missing layer is a run coordinator and persisted run registry.

The plan should also not assume that a fully separate persistence model is required from day one. A minimal run registry table plus the existing event log and artifact store is enough for the first slice.

## Implementation plan

### Phase 1 — Define the minimal UI runtime contract

Create a narrow contract for UI-driven workflow execution.

Required operations:

- start_run(workflow_path, context)
- get_run(run_id)
- observe_run(run_id)
- cancel_run(run_id)
- respond_approval(run_id, choice)
- get_artifacts(run_id)

Response fields should include:

- run_id
- execution_id
- status
- started_at / completed_at
- active_controls
- pending_approval_id
- last_event_id
- artifact references

This should remain a thin JSON contract and should not expose internal Python objects.

### Phase 2 — Add a persisted run registry in Hermes Core

Implement a small run registry that stores UI lifecycle state for each workflow execution.

Responsibilities:

- create a run record when a workflow starts
- keep a stable run_id that is independent of the internal execution_id if needed
- store status, timestamps, active controls, and approval state
- persist the last observed event cursor
- allow later requests to resolve the same run record

This registry should be backed by SQLite or another lightweight local store. It should not rely only on in-memory service state.

### Phase 3 — Refactor the runtime service into a stateful coordinator

Refactor [engine/runtime_service.py](engine/runtime_service.py) so it no longer behaves like a one-shot helper for every call.

The first slice should follow this pattern:

- start_run creates a run record and starts execution in a way that can be observed later
- the bridge returns immediately with a pending or running status
- observe_run reads run state and event history from the persisted registry plus the kernel event log
- respond_approval updates the run and resumes execution from the existing kernel state

Important design constraint:

- start_run should not block until the workflow completes
- the first implementation can use a simple background worker or thread for execution, but it must not make the HTTP request path depend on a long-running synchronous execution loop

### Phase 4 — Reuse the existing kernel as the execution engine

Do not duplicate workflow execution logic in the bridge.

The bridge should call the existing kernel methods to:

- start the workflow
- schedule the next steps
- advance execution
- handle approval transitions
- append artifacts and events

The bridge should only own UI-facing lifecycle orchestration and persistence.

### Phase 5 — Wire WebUI through the adapter seam

Use the existing WebUI runtime adapter seam rather than introducing a parallel code path.

The first adapter slice should support:

- start_run
- observe_run
- get_run
- respond_approval
- get_artifacts

Cancel can be added after the first slice if the underlying workflow control semantics are clearly defined.

### Phase 6 — Add tests around the bridge boundary

Add regression tests around the new boundary before broader rollout.

Coverage should include:

- successful run start and immediate status return
- progress polling and event replay
- approval path for a paused workflow
- artifact retrieval
- invalid workflow input
- unavailable bridge behavior

### Additional WebUI control surfaces to add after the first slice

The first slice should stay narrow, but the WebUI adapter contract also expects a few richer control methods once the basic bridge is stable. These are the next capabilities to add after start/observe/approval/artifact support is working:

- cancel_run / cancel_workflow
- respond_clarify
- queue_message
- update_goal
- append_event
- complete_run / finalize_run
- a generic handle(payload) dispatcher so the bridge can tolerate multiple core API shapes without hard-coding every method name

These methods are important for a more complete hybrid workflow experience, but they should be layered on after the core run registry and approval flow are proven. They should not block the first slice.

## Likely failure points

These are the main ways this work could fail if the plan is implemented literally as written:

1. Treating start_run as a synchronous “run to completion” operation
   - this would make the UI hang and would not support polling or hybrid control

2. Relying only on in-memory service state
   - a restarted process or a second request would lose track of the run

3. Duplicating the kernel’s execution logic in the bridge
   - this would create drift and make the architecture harder to reason about

4. Expecting approval semantics to be handled by the CLI layer
   - the UI bridge must expose approval control directly rather than relying on a separate CLI workflow

5. Trying to support too much at once
   - clarify, queue, and goal-style controls should come after the first slice of start/observe/approval/artifact support is stable

## Recommended implementation order

1. Define the minimal UI contract.
2. Add a persisted run registry in Hermes Core.
3. Refactor the runtime service into a stateful coordinator.
4. Reuse the existing kernel for execution advancement.
5. Wire WebUI through the adapter seam.
6. Add tests and enable the path behind a feature flag.

## Scope for the first slice

The first slice should stay intentionally narrow:

- one deterministic workflow
- one start/status/artifact flow
- one approval/resume control path
- one success case and one clear failure case

## Definition of done

This work is complete when:

- Hermes WebUI can start a workflow through Hermes Core
- the UI can observe progress and retrieve artifacts
- approval and resume work through the same runtime boundary
- the existing WebUI path remains intact when the integration is disabled
- the new path is covered by tests

## Future phases

Once the first slice is proven, the next phases can expand the bridge in a controlled way:

- richer control support for cancel, clarify, queue, and goal updates
- a more expressive event model for streaming and UI state synchronization
- optional support for multi-step or long-running workflows with better run lifecycle observability
- deeper policy and approval workflows that go beyond the initial approval checkpoint
- eventual support for more autonomous agent-like behaviors, but only after the runtime bridge and control contract are stable
