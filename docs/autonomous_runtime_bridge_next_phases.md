# Autonomous Runtime Bridge: Next-Phase Implementation Plan

## Goal

Evolve the first-slice Hermes Core runtime bridge from a UI-friendly lifecycle wrapper into a more agent-friendly coordination layer for long-running, partially autonomous workflows.

The priority is not to build a second runtime engine. The priority is to make the existing Hermes Core execution kernel easier to drive from Hermes Agent and Hermes WebUI through a stable, event-oriented contract.

## Current foundation

The current bridge already has the right shape for the first slice:

- a persisted run registry,
- a stateful runtime service,
- an API boundary for start/observe/approval/artifact flows,
- an adapter boundary for UI-style requests,
- and a kernel that already understands workflow execution, events, approvals, and artifacts.

The next step is to extend that boundary so it can support richer control and more explicit agent semantics without moving execution ownership out of Hermes Core.

## Contract alignment review

This plan should stay tightly aligned with the existing contracts rather than inventing a new runtime model.

### What the contracts already imply

- The workflow contract treats workflow definitions and workflow executions as separate concerns. The bridge must preserve that boundary and should not let agent or UI state replace execution state.
- The execution contract treats execution as the unit of control. The bridge should therefore expose run-level lifecycle operations, but it should still map them to underlying step and workflow executions rather than creating a parallel execution model.
- The event contract treats the event log as the source of truth. Any new bridge-level actions should be represented as events or should at least update the runtime in ways that can be replayed.
- The artifact contract treats artifacts as immutable lineage objects. The bridge must never mutate artifacts in place or treat UI state as authoritative over artifact history.

### What this means for the next phases

- The bridge can grow richer, but it should remain a coordination layer over the existing kernel rather than a replacement for it.
- Agent-facing context should be stored as run metadata or execution context, not as a second execution authority.
- Control actions such as approve, clarify, or queue should be expressed as explicit transitions that the kernel can understand and replay.

## Design principles

1. Hermes Core remains the authority for execution.
   - It owns workflow advancement, replay, artifacts, approvals, and policy evaluation.

2. Hermes Agent and Hermes WebUI remain thin collaborators.
   - They provide context, interaction, and human-facing control, but they do not own runtime state.

3. The bridge should be event-driven and resumable.
   - Every significant lifecycle transition should be represented as an event that can be replayed or observed.

4. The contract should stay narrow and explicit.
   - Avoid turning the bridge into a generic task queue or a full agent runtime.

5. The bridge must preserve the separation between run state, execution state, and agent context.
   - Run state is the UI or agent lifecycle view.
   - Execution state is Hermes Core’s authoritative workflow state.
   - Agent context is auxiliary metadata for reasoning and handoff.

## Phase 2 — Add a richer control plane

### Objective

Make the bridge capable of handling more than one approval checkpoint and more than one control action.

### Capabilities to add

- cancel_run
- clarify_request
- queue_message
- update_goal
- append_event
- finalize_run
- generic dispatch via a single handle(payload) entrypoint

### Contract shape

Introduce a normalized action envelope for these controls:

- action: the requested control operation
- run_id: the durable run identifier
- payload: action-specific data
- actor: optional identifier for the requester
- correlation_id: optional id to tie related events together

### Important implementation constraint

The dispatcher should be protocol-level, not a replacement for workflow semantics. Each action should map to a concrete runtime transition that is either already supported by the kernel or can be modeled as an explicit event plus a state update.

### Implementation seams

- [engine/http_adapter.py](engine/http_adapter.py): normalize incoming request shapes and route them through a generic dispatcher.
- [engine/api.py](engine/api.py): expose a stable control wrapper that accepts the action envelope.
- [engine/runtime_service.py](engine/runtime_service.py): implement the control actions against the existing kernel state rather than as a parallel workflow engine.

### Acceptance criteria

- the bridge accepts multiple control actions through one interface,
- the same run can be resumed or redirected after an approval or clarification step,
- control actions do not break the existing workflow state model.

## Phase 3 — Upgrade the event model for agent-style progress

### Objective

Move from simple status polling to a richer, semantically meaningful event stream.

### Event categories to define

- RUN_CREATED
- RUN_STARTED
- RUN_PAUSED
- RUN_RESUMED
- CONTROL_REQUESTED
- CONTROL_RESPONDED
- GOAL_UPDATED
- MESSAGE_ENQUEUED
- ARTIFACT_ADDED
- RUN_FINALIZED

### Event payload requirements

Each event should include:

- run_id,
- execution_id,
- timestamp,
- actor or source,
- correlation_id,
- status after the event,
- optional control metadata.

### Implementation seams

- [engine/models.py](engine/models.py): extend the event payload or run-state model with explicit control metadata if needed.
- [engine/storage.py](engine/storage.py): ensure run history and event cursors are durable and replayable.
- [engine/runtime_service.py](engine/runtime_service.py): emit the richer events from the existing lifecycle transitions.

### Acceptance criteria

- the UI or agent can subscribe to meaningful progress events instead of relying only on coarse status values,
- the system can reconstruct run state from the persisted event stream,
- internal events remain consistent with the run registry.

## Phase 4 — Add agent-facing run context

### Objective

Make the run contract more useful for Hermes Agent without giving the agent ownership of workflow execution.

### Proposed context fields

- session_id or conversation_id
- agent_goal
- user_intent_summary
- workflow_template or definition reference
- runtime hints such as preferred approval role or timeout policy
- last_user_message or requester context

### Design constraint

These fields should be treated as context, not as a substitute for the workflow kernel.

Hermes Agent can provide them, but Hermes Core remains the execution authority.

### Implementation seams

- [engine/models.py](engine/models.py): add a lightweight execution context envelope for agent-side metadata.
- [engine/runtime_service.py](engine/runtime_service.py): include context in run creation and state observation.
- [engine/api.py](engine/api.py): expose the context through the public interface.

### Acceptance criteria

- a run launched from Hermes Agent can carry enough context for later resume or summarization,
- the bridge can distinguish between execution state and agent-facing conversation state,
- the design stays compatible with existing deterministic workflows.

## Phase 5 — Strengthen durability and recovery

### Objective

Make the bridge resilient to restarts, retries, and partial progress.

### Work items

- add idempotency for repeated start or resume requests,
- persist enough state to recover a run after a process restart,
- add heartbeat or liveness information for long-running runs,
- ensure repeated control actions are safe and observable.

### Implementation seams

- [engine/storage.py](engine/storage.py): strengthen run record persistence and indexing.
- [engine/runtime_service.py](engine/runtime_service.py): protect state transitions with clear serialization and recovery rules.
- [engine/api.py](engine/api.py): expose explicit error semantics for duplicate or stale control actions.

### Acceptance criteria

- a run can be resumed after the process restarts,
- duplicate control requests do not corrupt workflow state,
- the bridge returns clear, deterministic error responses for invalid transitions.

## Phase 6 — Integrate the contract across both clients

### Objective

Use the same bridge contract from both Hermes Agent and Hermes WebUI.

### Work items

- keep the adapter boundary in Hermes WebUI aligned with the core contract,
- add a small agent-side handoff helper that uses the same action envelope,
- keep the old path available behind a feature flag so the legacy experience remains intact.

### Implementation seams

- [engine/http_adapter.py](engine/http_adapter.py) for the core-side transport boundary,
- the Hermes WebUI adapter seam for the UI side,
- the Hermes Agent handoff layer for conversational entrypoints.

### Acceptance criteria

- both clients can use the same run lifecycle shape,
- the core contract remains stable even when the UI or agent implementations evolve.

## Failure points and mitigations

The main risks are not in the idea of the bridge itself; they are in letting the bridge blur boundaries that the contracts keep separate.

1. Overloading the bridge with agent ownership
   - Risk: Hermes Agent or Hermes WebUI begins treating the bridge as if it were the real execution engine.
   - Mitigation: keep execution state inside Hermes Core and ensure the bridge only orchestrates and observes.

2. Treating UI state as authoritative over artifacts
   - Risk: the run record or a UI-side payload mutates artifact lineage or overwrites core execution results.
   - Mitigation: artifact writes must remain inside the kernel and the artifact contract; the bridge can only reference or surface them.

3. Making control actions too ad hoc
   - Risk: clarify, queue, or goal updates become free-form side effects that do not map to explicit workflow transitions.
   - Mitigation: each control action should be modeled as an explicit transition or event with a clear contract and an error path.

4. Allowing duplicate or concurrent control requests to race
   - Risk: repeated approve or cancel calls can interleave and corrupt the run state.
   - Mitigation: serialize state transitions in the runtime service and make repeated actions idempotent or reject stale ones with deterministic errors.

5. Using loosely structured payloads for everything
   - Risk: the event stream becomes hard to replay or audit.
   - Mitigation: keep canonical event types, stable payload fields, and explicit correlation ids.

6. Letting run metadata leak into execution semantics
   - Risk: conversation context or UI hints start affecting scheduling and approvals in ways that surprise the kernel.
   - Mitigation: keep agent context separate from policy and execution decisions; it may influence presentation or handoff, but not bypass the execution contract.

## Recommended sequence

1. Add the generic control dispatcher and normalize control actions.
2. Introduce richer run events and event metadata.
3. Add agent-facing run context fields.
4. Strengthen durability and recovery semantics.
5. Roll out the shared contract across Hermes Agent and Hermes WebUI.

## Definition of done for the next phases

This work is complete when:

- the bridge supports richer controls beyond the first approval-only slice,
- runs can be observed through a meaningful event stream,
- Hermes Agent can hand off structured work with context without owning execution state,
- the runtime remains durable, recoverable, and testable,
- and the bridge stays compatible with the workflow, execution, event, and artifact contracts already defined in the repository.
