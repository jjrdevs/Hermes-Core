# Autonomous execution loop implementation plan

## Goal

Make Hermes Core capable of handling a spec-driven coding task for a moderate duration without constant human supervision. The system should inspect the workspace, make changes, run verification, recover from simple failures, and return a useful summary.

## Why this matters

The current execution path in [workers/local_worker.py](workers/local_worker.py) is effectively a single-shot prompt-to-artifact step. That is enough for a proof of concept, but it is not yet robust enough for unattended progress on real tasks.

## Proposed architecture

### Core components

- Task envelope: a structured request that contains goals, constraints, acceptance criteria, and allowed tools.
- Execution supervisor: an orchestration layer that runs one step at a time and persists progress after each iteration.
- Tool runner: a thin wrapper for shell commands, file edits, test execution, and artifact inspection.
- Verifier: runs tests, linters, builds, or other checks and returns structured output.
- Recovery policy: decides whether to retry, escalate, or stop when a step fails.
- Checkpoint store: persists the latest state so the run can resume later.

### External acceleration strategy

- Use Aider as the first implementation path for patch generation and repo-aware verification if the goal is to ship quickly.
- Use OpenHands or OpenDevin as reference implementations for the tool loop, sandbox boundaries, and agent step structure.
- Keep Hermes Core as the orchestrator and state store; do not build a full autonomous coding loop from first principles if an existing runtime can be wrapped behind the tool interface.

## Implementation steps

### Phase 1: Introduce a task execution contract

- Add a task/message envelope that carries:
  - objective
  - success criteria
  - constraints
  - allowed tools
  - max runtime and retry budget
- Store the envelope with the workflow execution state in [engine/models.py](engine/models.py).

### Phase 2: Add an execution supervisor

- Build a supervisor around the current runtime in [engine/runtime.py](engine/runtime.py) and [engine/runtime_service.py](engine/runtime_service.py).
- The supervisor should manage:
  - one active step at a time
  - a loop of plan → act → verify → next step
  - checkpointing after each iteration

### Phase 3: Add real execution tools

- Implement wrappers for:
  - file read/write/edit
  - shell command execution
  - test execution
  - repository diff inspection
- Keep these behind a stable tool interface so the supervisor can call them without hard-coding behavior.

### Phase 4: Add verification and recovery

- Introduce a verification stage that runs the relevant checks and returns structured results.
- If verification fails, the supervisor should:
  - retry once for transient issues
  - inspect the failure output
  - adjust the plan or request a stronger model for the next step
- Stop early with a clear summary if the task is blocked.

### Phase 5: Persist checkpoints and resumability

- Save execution state after each loop iteration.
- Resume from the last checkpoint instead of restarting from scratch.
- Emit a compact summary artifact that tells the user what progress was made and what remains.

## Deliverables

- A task execution contract
- An execution supervisor service
- A tool runner abstraction
- A verification and retry layer
- Checkpoint/resume support
- A report artifact for each completed or partially completed run

## Acceptance criteria

- Given a simple feature request with a clear spec, the system can make changes, run a relevant verification step, and return a helpful summary.
- If a verification step fails, the system can make at least one corrective pass before giving up.
- The system can resume from the last saved checkpoint after interruption.

## Risks and mitigations

- Risk: the loop becomes too chatty or too expensive.
  - Mitigation: cap iterations, enforce timeouts, and require clear success criteria.
- Risk: the system makes bad edits and loops on the same failure.
  - Mitigation: require a verification step before continuing and impose retry limits.
