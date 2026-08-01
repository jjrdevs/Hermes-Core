# Scheduled background runtime implementation plan

## Goal

Allow Hermes to run useful work while the user is away. The system should be able to start a task, make progress, save checkpoints, and return a clear summary when the user comes back.

## Why this matters

This is the key use case for unattended value: not perfect autonomy, but steady progress while you sleep or are otherwise unavailable. It should be robust enough to start, continue, and stop safely without losing the work already done.

## Proposed architecture

### Core components

- Job scheduler: launches tasks on a schedule or on demand.
- Job runner: executes a single task and persists state throughout.
- Checkpoint store: saves progress after each meaningful step.
- Run summary artifact: produces a compact report of progress and blockers.
- Notification layer: reports completion or partial results to the user.

## Implementation steps

### Phase 1: Introduce a job model

- Define a job record with:
  - task spec
  - desired runtime budget
  - schedule or trigger
  - current status
  - checkpoint reference
- Store jobs and run state in the existing persistence layer.

### Phase 2: Add a scheduler service

- Implement a lightweight scheduler that can:
  - start jobs on a cron-like interval
  - start jobs manually
  - stop jobs cleanly after a time budget is exhausted
- Keep the scheduler separate from the execution engine so it stays simple.

### Phase 3: Add resumable execution

- Persist progress after each iteration.
- Support restart from the latest checkpoint if the process is interrupted.
- Write a run summary artifact and an explicit status field for the current job.

### Phase 4: Add progress reporting

- Emit progress updates that are easy to read later.
- Include:
  - what changed
  - what was verified
  - what remains to do
  - whether the task completed fully or only partially

### Phase 5: Add notification and handoff

- Produce a summary artifact for the user to inspect.
- Optionally send a simple notification or write a report file to a known location.
- Keep the handoff format simple so the user can resume quickly.

## Deliverables

- Job and run state model
- Scheduler service
- Checkpointed execution flow
- Progress summary artifact
- Manual resume and restart path

## Acceptance criteria

- A job can be scheduled and start automatically.
- The system can persist progress and resume later.
- If the runtime budget is reached, the system stops cleanly and reports the latest state.
- The user can inspect the last progress summary and decide whether to continue.

## Risks and mitigations

- Risk: jobs become too long and consume too much runtime.
  - Mitigation: enforce runtime budgets and intermediate checkpoints.
- Risk: partial progress is hard to interpret.
  - Mitigation: require a structured summary artifact after each run or interruption.
