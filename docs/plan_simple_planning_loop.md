# Implementation Plan: Simple Planning Loop

## Objective

Add a lightweight planning loop that makes the agent behave more intelligently without turning Hermes Core into a full general-purpose agent runtime. The loop should be simple, explicit, and easy to reason about:

plan -> act -> observe -> revise

## Why this matters

The current runtime can execute steps and produce artifacts, but it still lacks a structured loop that says: “first make a plan, then execute against it, then inspect the outcome, then revise if necessary.” This is the single biggest upgrade for autonomy because it changes the behavior from reactive execution to deliberate progress.

## Design goals

1. Keep the loop explicit and observable.
2. Make planning outputs durable and inspectable.
3. Avoid over-engineering the first version.
4. Reuse the existing workflow and run lifecycle rather than inventing a parallel state machine.

## Proposed architecture

### 1. Planner step

Before executing the main body of work, create a lightweight plan object containing:

- objective
- subgoals
- candidate tools or actions
- expected outputs
- success criteria
- failure hypotheses

This plan should be stored as a run-level artifact or event payload, not as a hidden model-side thought.

### 2. Execution step

The executor should carry out the planned actions in sequence or in a controlled batch. Tool outcomes should feed back into the loop.

### 3. Observation step

After each execution chunk, collect:

- tool results
- artifacts created
- errors encountered
- progress against the plan

This should be represented as a structured observation event.

### 4. Revision step

If the observation shows the plan is failing or incomplete, the runtime should revise the plan. Revision can be simple at first:

- retry the failed action with a different tactic
- change the next subgoal
- request human input if the plan is blocked

## Implementation approach

### Phase 1 — Introduce a planning artifact and state

- add a lightweight plan object to the run context in [engine/models.py](../engine/models.py)
- store the plan as part of the run state in [engine/runtime_service.py](../engine/runtime_service.py)
- emit a planning event when the plan is created

Acceptance criteria:

- each run can carry a plan object
- the plan is visible through the run state and event stream

### Phase 2 — Add plan execution checkpoints

- define a simple execution loop over subgoals
- each subgoal should correspond to one or more tool actions
- after each subgoal, evaluate whether execution proceeded as expected

Acceptance criteria:

- the runtime can progress through a plan in discrete stages
- plan progress is observable and resumable

### Phase 3 — Add revision logic

- if a subgoal fails, revise the remaining plan
- if too many failures occur, mark the run as blocked or require approval
- preserve the revision history in run events

Acceptance criteria:

- a failed subgoal triggers a structured revision path
- the system does not silently continue after a meaningful failure

### Phase 4 — Add planner hints from the model layer

Hook the planning loop into the model adapter layer so the model can produce a structured plan when the capability profile says planning is available. The existing capability hints in [workers/model_adapter.py](../workers/model_adapter.py) provide the right seam for this.

Acceptance criteria:

- planning is available when the model adapter advertises it
- the plan is generated from model output rather than hardcoded logic

## Files to change

- [engine/models.py](../engine/models.py)
- [engine/runtime.py](../engine/runtime.py)
- [engine/runtime_service.py](../engine/runtime_service.py)
- [workers/model_adapter.py](../workers/model_adapter.py)
- [engine/api.py](../engine/api.py)

## Suggested loop semantics

A minimal first version can use this structure:

1. create plan
2. execute next subgoal
3. observe outcome
4. if successful, advance
5. if failed, revise or stop
6. repeat until complete or blocked

This is intentionally simple and should be enough to make the agent feel deliberate rather than purely reactive.

## Tests

Add tests for:

- planning artifact creation
- plan progression through subgoals
- revision after a failed subgoal
- blocked state after repeated failures
- replay of plan state across restart recovery

## Estimated effort

- 1 to 3 weeks for a basic loop
- 3 to 5 weeks for a more capable loop with revision policy and better model integration

## Definition of done

This work is complete when a run can carry a structured plan, execute against it in stages, observe outcomes, and revise the plan when execution deviates from expectations.
