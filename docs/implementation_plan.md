# Hermes Core Implementation Plan

## Goal

Define the first vertical slice for Hermes Core that proves the runtime architecture without introducing a generic agent framework.

Hermes should execute an authorized workflow, manage runtime state, persist events and artifacts, assign workers and models by capability, and preserve the operational/runtime separation.

## Scope for the first vertical slice

1. Minimal workflow definition and execution separation
2. Event-sourced runtime state with append-only event log
3. Immutable artifact model with content and artifact hashing
4. Simple execution lifecycle and scheduler
5. Worker contract and a minimal worker implementation
6. Model adapter contract with a stub provider
7. SQLite persistence for events and artifact metadata
8. Minimal CLI or script to execute a sample workflow

## Non-goals for this slice

- No generic autonomous planning or goal decomposition
- No complex tool orchestration or multi-tool policy enforcement
- No full approval workflow or human-in-the-loop gating
- No provider-specific model features exposed in core contracts
- No distributed execution or external service orchestration

## Minimal vertical slice use case

A sample workflow is defined, Hermes creates a workflow execution, schedules the first step, resolves a worker and a model adapter, executes the worker, stores artifacts and events, and completes the workflow.

This proves the core runtime loop and the architecture without premature abstraction.

## Components

### 1. Core runtime engine

- `engine/` handles workflow execution, scheduling, event handling, and state reconstruction.
- It owns the runtime state and decides transitions.
- It derives current state from the event log on startup.

### 2. Workflow and execution contracts

- Workflow definitions are immutable and only contain declared steps, transitions, and policy refs.
- Workflow executions are runtime-only and store status, active executions, completed executions, events, and policy context.
- Execution instances are the unit of control.

### 3. Event log and persistence

- Use SQLite for the first persistence layer.
- Persist events in an append-only event log.
- Persist artifacts and metadata separately so artifacts remain immutable and traceable.
- On restart, Hermes rebuilds workflow/execution state by replaying events.

### 4. Artifact lineage and immutability

- Artifacts include `content_hash`, `artifact_hash`, `version`, `parent_version`, and `decision_record`.
- Hermes stores artifacts as immutable records.
- New revisions become new artifact versions.

### 5. Worker contract and execution

- Workers receive a `WorkerRequest` from Hermes and return a structured `WorkerResponse`.
- Workers do not own workflow state or schedule themselves.
- Hermes resolves the worker and model by declared capabilities.
- The first worker implementation can be a simple local worker that uses a stubbed model adapter.

### 6. Model adapter contract

- Define `generate(prompt, ...)` and `capabilities()` only.
- Keep the adapter interface minimal and Hermes-focused.
- Delay provider-specific features until implementation experience is available.

### 7. Tool/Policy stub

- Implement a minimal tool contract and policy guard.
- For the first slice, tool access can be a stub that allows execution while leaving room for later authorization enforcement.
- Ensure the runtime model keeps tool invocation under Hermes control.

### 8. Example sample workflow

Create a simple workflow such as:

- Step 1: `architect` produces `architecture_v1`
- Step 2: `developer` depends on `architecture_v1` and produces `implementation_patch_v1`
- Step 3: complete workflow

The runtime should:

- create the workflow execution
- schedule step 1
- assign a worker and a model adapter
- run the worker and create an artifact
- append the corresponding events
- schedule step 2 after dependency satisfaction
- complete the workflow

## Implementation milestones

1. Define runtime contracts and data shapes
   - workflow definition/execution
   - execution lifecycle
   - event schema
   - artifact schema
   - worker request/response
   - model adapter

2. Implement persistence abstractions
   - SQLite-backed event log
   - artifact metadata + content store
   - simple schema and read/write helpers

3. Implement runtime state reconstruction
   - load event history
   - derive workflow and execution state
   - verify deterministic state from events

4. Implement scheduler and execution loop
   - create workflow execution from definition
   - open initial step executions
   - resolve workers/models by capability
   - append lifecycle events

5. Implement a minimal worker adapter
   - execute a step using a model adapter stub
   - return artifacts and recommendations
   - persist created artifacts and associate them with execution

6. Add a sample workflow runner
   - define a simple workflow in YAML or JSON
   - run it through Hermes
   - produce output artifacts and event log entries

7. Validate the vertical slice
   - verify workflow executes end-to-end
   - verify state rebuilds from events
   - verify artifacts are immutable and hashed
   - verify worker/model selection is Hermes-driven

## Acceptance criteria

- A workflow definition is separate from runtime execution.
- Hermes persists all state-changing operations as events.
- Hermes reconstructs runtime state from replaying events.
- Artifact records are immutable and include lineage hashes.
- Workers are assigned by capability, not by provider-specific logic.
- Models are accessed through a minimal adapter interface.
- The first vertical slice runs a simple workflow end-to-end.

## Next step after the vertical slice

- Add policy enforcement for tool access and approvals.
- Implement additional worker capability adapters.
- Introduce real model provider adapters.
- Expand workflow transitions, conditional branches, and approval gates.
- Refine persistence abstractions for future backend swaps.
