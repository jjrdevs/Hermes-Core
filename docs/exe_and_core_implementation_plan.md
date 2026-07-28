# Hermes Core + Hermes Agent + Hermes WebUI Integration Plan

## Objective

Wire Hermes Agent and Hermes WebUI to Hermes Core as a thin, opt-in execution bridge so complex, stateful tasks can be handed off into Hermes Core without breaking the existing chat and UI experience.

## Why this plan is different

The earlier approach was too broad and would have been fragile because it tried to make the UI and agent “own” the runtime. That is the wrong boundary. The repo already has the real execution engine in Hermes Core and the right adapter seams in Hermes WebUI; the missing piece is a narrow integration layer.

## Key pitfalls to avoid

The plan below avoids the failure modes that would make this brittle:

- Do not make Hermes Agent or Hermes WebUI import Hermes Core internals directly. That would create tight coupling and make future changes harder.
- Do not replace the current agent/UI experience. Keep the legacy path intact and make the new bridge opt-in and fail-closed.
- Do not move workflow ownership into the UI. Hermes Core must remain the authoritative runtime for execution, replay, artifacts, and approvals.
- Do not assume the three repositories share the same runtime environment. Use a process boundary or a small local transport layer instead of deep Python coupling.
- Do not try to support every workflow type at once. Start with structured, deterministic workflows and a narrow handoff contract.

## Guiding principles

- Hermes Core remains the source of truth for workflow execution, replay, artifacts, approvals, and persistence.
- Hermes Agent and Hermes WebUI remain responsible for conversation, session state, and user interaction.
- The integration must be thin, explicit, and feature-flagged.
- The default experience must continue working even if the bridge is disabled or unavailable.
- Each integration step must be backed by tests before enabling it by default.

## Phase 0 — Lock the contract before wiring anything

### 1. Define the handoff contract
Create a small contract shared by Hermes Agent and Hermes WebUI:

- input: task description, workflow template or workflow path, optional context payload, runtime config
- output: execution id, status, artifact references, and approval state if relevant

Use a JSON-like request/response shape so the bridge can stay stable even if the internal runtime changes.

### 2. Verify the existing runtime seams
Use the current runtime surfaces as the baseline:

- [hermes-core/engine/runtime_service.py](hermes-core/engine/runtime_service.py)
- [hermes-core/engine/api.py](hermes-core/engine/api.py)
- [Applications/hermes-webui/api/runtime_adapter.py](Applications/hermes-webui/api/runtime_adapter.py)
- [Applications/hermes-webui/api/agent_runtime.py](Applications/hermes-webui/api/agent_runtime.py)

Do not invent a new execution API. Build around the seams that already exist.

## Phase 1 — Build the bridge boundary

### 3. Add a thin Hermes Core bridge
Implement a small bridge layer that exposes Hermes Core to Agent and WebUI over a stable boundary.

Recommended shape:

- use the existing Hermes Core CLI or packaged binary as the process boundary
- pass requests as subprocess calls or a small local HTTP endpoint
- keep the bridge responsible for starting workflows, polling status, resolving artifacts, and handling approval/resume state

This is safer than direct Python imports because it avoids cross-repo environment and import coupling.

### 4. Keep the bridge fail-closed
If the bridge is unavailable, the agent/UI must fall back to the existing non-structured path rather than failing hard.

This is essential for preserving the current chat experience.

## Phase 2 — Connect Hermes Agent

### 5. Add an agent-side handoff helper
Create a small agent-side helper that can:

- detect when a task is multi-step, approval-heavy, or stateful
- hand off to Hermes Core through the bridge
- receive an execution id and summary back
- surface the result in the session without taking ownership of the workflow state

The first implementation should be conservative: only enable it for a clearly defined class of tasks.

### 6. Preserve the existing agent workflow
The agent should continue to work as usual for normal chat tasks. The new bridge should only be used when the task is a good fit for structured execution.

## Phase 3 — Connect Hermes WebUI through the runtime adapter seam

### 7. Implement a Hermes Core adapter in WebUI
Use the existing adapter seam in [Applications/hermes-webui/api/runtime_adapter.py](Applications/hermes-webui/api/runtime_adapter.py) and add a concrete adapter for Hermes Core.

The adapter should implement the minimal methods needed for the first slice:

- start a run
- observe progress
- fetch status
- request approval or resume control
- expose artifacts and terminal state

### 8. Make WebUI integration opt-in
Use an environment flag such as a runtime adapter mode so the new path is off by default until it is proven.

That prevents a broken integration from taking down the whole UI experience.

## Phase 4 — Add tests before enabling the path by default

### 9. Add bridge tests first
Add tests around:

- successful workflow handoff
- status polling
- artifact retrieval
- approval/resume handling
- failure when the bridge is unavailable
- invalid workflow input

### 10. Add WebUI and agent regression tests
Test the adapter boundary from the UI and the handoff boundary from the agent, but keep the tests focused on the contract rather than on internal engine details.

## Phase 5 — Roll out narrowly and document it

### 11. Start with one workflow shape
Support one narrow, deterministic workflow path first, such as:

- task intake
- workflow creation in Hermes Core
- progress polling
- approval or completion
- artifact summary

Do not try to support arbitrary agent behavior in the first release.

### 12. Document the assumptions clearly
Document:

- how to enable the bridge
- what workflows are supported
- where the data directory lives
- how failures fall back to the legacy path

## Delivery milestones

### Milestone A — Bridge is stable
- Hermes Agent can hand off a workflow to Hermes Core and receive an execution id.
- Hermes WebUI can start and observe a workflow through the adapter seam.
- The legacy experience still works when the bridge is disabled.

### Milestone B — One structured workflow path works end to end
- a sample workflow can be launched from the agent or UI
- progress and artifacts can be observed
- approvals or resume flow behave correctly

### Milestone C — Integration is release-ready
- tests cover the bridge and adapter
- documentation is in place
- the integration can be enabled with a feature flag and safely rolled back

## Recommended implementation order

1. Lock the handoff contract.
2. Add the Hermes Core bridge boundary.
3. Connect Hermes Agent with a conservative handoff helper.
4. Connect Hermes WebUI through the runtime adapter seam.
5. Add tests and only then enable the path by default.

## Definition of done

The integration is done when:

- Hermes Agent can hand off a structured task to Hermes Core without taking ownership of execution state.
- Hermes WebUI can start and observe the same workflow through the adapter seam.
- The existing chat and UI experience remains intact when the bridge is unavailable.
- The bridge is covered by tests and documented clearly.
