# Hermes Core roadmap for unattended coding progress

## Goal

Turn Hermes Core into a useful unattended coding assistant for narrow, spec-driven tasks. The first milestone should be a system that can start a task, make progress, save state, verify results, and hand back a useful summary.

## How the roadmap and implementation plans fit together

The roadmap is the high-level plan. The implementation plans are the detailed design documents for each major workstream.

In practice:

- Use the roadmap to decide what comes first and what the overall sequence is.
- Use the implementation plans when you are ready to build a particular phase in detail.
- The roadmap should be your guide for ordering work; the implementation plans should be your guide for execution details.

## Audit findings and corrections

The first draft was too optimistic in three ways:

- It implied that full autonomous coding should be built from scratch in one pass. That is a common failure point for agent systems.
- It did not include enough guardrails for safety, rollback, and verification. Those are essential if the system is allowed to edit files or run commands.
- It assumed that every repository would have a clear validation command and a stable execution environment. In practice, that is often false, so the plan needs a fallback path.

The corrected plan below narrows the scope and makes the risks explicit.

## Guiding principle

Favor small, verifiable steps over broad ambition. The first version should be good at a few things well:

- take a clear task spec
- make progress on that task
- write useful artifacts or code changes
- verify results with a simple, repo-specific check
- stop cleanly and report what happened

## Phase 0: Prove a narrow vertical slice

### Objective

Make sure the base runtime is stable before adding autonomous behavior.

### Deliverables

- Keep workflow execution, event replay, artifact persistence, and worker contracts working reliably.
- Ensure the runtime can execute one simple end-to-end workflow without regressions.
- Add regression tests for workflow execution and artifact persistence.
- Define a small allowlist of safe commands and file operations for the first milestone.
- Add a dry-run mode so the system can preview actions before applying them.
- Keep network access disabled by default for the first milestone unless the task explicitly requires it.

### Why first

If the base runtime is shaky, every higher layer will be harder to trust.

## Phase 1: Build an execution loop with verification

### Objective

Replace the current one-shot worker behavior with a simple multi-step execution loop that plans, acts, verifies, and stops safely.

### Deliverables

- Task execution contract
- Supervisor that runs plan → act → verify → next step
- Tool runner for shell commands and file operations
- Verification step that runs a repo-specific check such as a test or lint command
- A diff preview and review step before applying changes
- Clear stop conditions, retry limits, and a rollback path
- A fallback state when no suitable verification command exists: mark the task as “not verified” rather than claiming success

### Success criteria

Given a simple spec, the system can:

- inspect the repository
- make one or more changes
- run a relevant verification command
- produce a summary of what changed
- stop cleanly if the task is blocked or the verification fails

## Phase 2: Add resumable checkpoints

### Objective

Make the system useful while you are away.

### Deliverables

- Save progress after each loop iteration
- Persist the current task state and partial artifacts
- Resume from the latest checkpoint after interruption
- Write a concise progress summary artifact
- Record a clear reason for any stop, retry, or escalation
- Keep checkpoints local and atomic so the system can recover without depending on a background daemon

### Success criteria

- A run can be interrupted and resumed later without starting over.
- The user can inspect what progress was made and what remains open.

## Phase 3: Add lightweight memory

### Objective

Let the system remember useful lessons, notes, and prior outcomes without overcomplicating the first version.

### Memory strategy

For version 1, memory should be built as a small local system rather than pulled in from an external repo. The simplest approach is:

- SQLite or JSON-backed storage for structured notes and task summaries
- keyword and metadata retrieval rather than embeddings at first
- a small retention policy so memory remains bounded

This keeps the system easy to operate and avoids introducing another dependency stack too early. If later iterations need stronger semantic recall, an external memory system such as Mem0 could be added as an adapter, but it should remain optional rather than required.

### Deliverables

- Memory schema for notes, lessons, and task summaries
- Ingestion of useful runtime signals
- Retrieval of relevant memories for new tasks
- Context assembly for task execution
- A simple retention policy so memory does not grow without bound
- Explicit rules to avoid storing secrets, tokens, or sensitive environment data

### Success criteria

- A later run can recall a previous lesson or approach instead of repeating the same mistake.
- The system can attach a small set of relevant memories to the next task.

## Phase 4: Add a cheap-first routing layer

### Objective

Use local or cheap models first, escalating only when the task needs more capability.

### Current status

A lightweight provider router and fallback policy are now implemented in [workers/model_adapter.py](workers/model_adapter.py) and used by [engine/runtime_service.py](engine/runtime_service.py). The router prefers cheaper available providers for simple tasks and falls back to a safe stub adapter when the preferred provider is unavailable.

### Current status

The execution loop in [engine/runtime_service.py](engine/runtime_service.py) now constructs a structured review plan alongside its summary artifact, giving the runtime a clearer plan-to-review handoff for each iteration. It can also apply a lightweight patch to a workspace file, include the resulting diff in the summary payload, and emit a structured verification report with attempts, retries, status, and command details.

### Deliverables

- Provider interface and registry
- Capability model for providers
- Router that chooses a provider based on task difficulty and cost
- Failover and health checks for provider errors
- Cost and usage reporting
- A simple configuration surface for provider preferences
- A default provider fallback so the system still works even if a preferred provider is unavailable

### Success criteria

- Simple tasks default to a cheap or local provider.
- Harder tasks escalate to a stronger provider when needed.
- Failed providers do not crash the whole run.

## Phase 5: Add scheduled background jobs

### Objective

Turn the system into a useful unattended worker.

### Current status

A simple background job scheduler is now implemented in [engine/scheduler.py](engine/scheduler.py) with a SQLite-backed job store. It supports creating jobs, running them manually, and executing due jobs on a simple interval schedule while persisting a summary and checkpoint-like payload. The CLI now exposes this functionality through a lightweight job subcommand for creating and listing jobs.

### Deliverables

- Job scheduler for recurring or one-off runs
- Run status tracking
- Progress reporting and summary output
- Optional notifications or report files
- A clean stop and resume path for long-running jobs
- A simple first version that supports one-shot background execution via CLI; full cron-style scheduling can be deferred

### Success criteria

- The system can launch work on a schedule or on demand.
- It produces visible progress and a summary even if the task is only partially complete.

## External repos and libraries to use

The plan should not build everything from first principles. The following projects would materially shorten delivery time and improve quality if used as integration points:

- Aider: use as the initial patching and verification backend for file edits, test runs, and repo-aware changes. This reduces the effort required to build a reliable editing loop.
- OpenHands or OpenDevin: use as design references and, if practical, as execution adapters for tool use and sandboxed workflows. They are strong references for agent-loop structure and tool orchestration.
- LiteLLM: use as the initial provider abstraction for model routing, fallbacks, and provider compatibility. This is a much faster path than building provider plumbing from scratch.

These should be used as accelerators, not as a replacement for Hermes Core’s orchestration model. Hermes Core should remain the state machine, workflow engine, checkpoint store, and policy layer.

## Out of scope for version 1

The first version should explicitly avoid:

- fully general autonomous debugging across large unfamiliar codebases
- multi-repo or multi-service orchestration
- arbitrary package installation without explicit approval
- long-horizon planning that requires many tool calls and many retries
- full web browsing or external automation beyond a narrow allowlist

## Critical risks and mitigations

- Risk: the system becomes too autonomous and makes unsafe edits.
  - Mitigation: enforce a command allowlist, require verification before claiming success, and keep a rollback path.
- Risk: the system spends too much money or time on verbose loops.
  - Mitigation: cap iterations, enforce budgets, and prefer smaller, targeted tasks.
- Risk: the system overfits to one repository or one task shape.
  - Mitigation: start with one narrow task type and one repo context before broadening scope.
- Risk: verification is too weak and produces false confidence.
  - Mitigation: require task-specific checks and surface both pass and failure output clearly.
- Risk: checkpoints and state become inconsistent.
  - Mitigation: persist state atomically and treat checkpoints as immutable snapshots.

## Suggested implementation order

1. Execution loop with verification
2. Checkpointing and resumability
3. Lightweight memory
4. Routing layer
5. Scheduled background jobs

These phases correspond to the detailed implementation plans below:

- Execution loop: [docs/implementation_plan_autonomous_execution_loop.md](docs/implementation_plan_autonomous_execution_loop.md)
- Routing layer: [docs/implementation_plan_model_routing_layer.md](docs/implementation_plan_model_routing_layer.md)
- Memory layer: [docs/implementation_plan_memory_layer.md](docs/implementation_plan_memory_layer.md)
- Scheduled background runtime: [docs/implementation_plan_scheduled_background_runtime.md](docs/implementation_plan_scheduled_background_runtime.md)

## How we will execute it

The work should happen in short, testable iterations rather than as one large build.

### Iteration 1: prove the vertical slice

- Implement a minimal task envelope and execution supervisor.
- Add a dry-run mode and a single safe tool runner.
- Make the system perform one simple repo-aware task end to end.
- Require a verification step and capture a clear pass/fail/not-run result.

### Iteration 2: make it resumable

- Add local checkpoint persistence after each loop step.
- Ensure the system can stop and resume without losing the last meaningful progress.
- Add a summary artifact that explains what happened and what remains.

### Iteration 3: add memory and recovery

- Add lightweight memory for notes, lessons, and task summaries.
- Use that memory in a narrow way during later runs.
- Add a retry policy and explicit stop conditions for repeated failures.

### Iteration 4: add provider routing

- Introduce a provider abstraction and a cheap-first router.
- Use LiteLLM for provider compatibility rather than building a custom stack from scratch.
- Keep the router simple and measurable.

### Iteration 5: add background execution

- Support one-shot background execution from the CLI first.
- Add status reporting and resumable job state.
- Defer full scheduling complexity until the core loop is already proven.

## Recommended scope for version 1

The first useful version should focus on:

- one clear task type
- one repository or project context
- one simple verification command
- one resumable loop
- one summary artifact

## Definition of done for version 1

A user can give Hermes a spec-driven task, leave it running, come back later, and find:

- a partial or complete change set
- a verification result that is clearly marked as pass, fail, or not run
- a summary of what was done
- a clear next step or blocked reason

That is the first milestone that meaningfully matches the unattended-progress goal.
