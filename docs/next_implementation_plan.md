# Hermes Core Next Implementation Plan

Date: 2026-07-31

## Purpose

Turn the current Hermes Core prototype into a dependable bounded repository-task runtime.

The next implementation cycle should prioritize trustworthy repeated execution over broader autonomy. Hermes should become excellent at small, spec-driven repository tasks before attempting long-horizon debugging, multi-repository work, or unrestricted automation.

## Implementation progress

- Slice 1 has started: repository tasks now carry a normalized persisted budget with iteration, correction, model-token, patch-file, and patch-byte limits.
- Patch file and byte budgets are enforced before mutation, and budget limits/usage are stored in task envelopes, summaries, and checkpoints.
- Task transition modeling is now implemented as a compatibility layer for repository tasks.
- Repository-task budget usage now records preview tool calls and elapsed wall time.
- Scheduler runtime budgets are now enforced around job executors.
- Run-scoped tool-call budgets now deny execution before tool runtime entry and expose usage through `get_run`.
- Scheduler now blocks concurrent starts of the same job and records `concurrent_execution_blocked` without disturbing the active execution.
- Shell timeouts now launch children in isolated Unix process groups and terminate descendants on timeout.
- Shared SQLite event-log access is serialized to protect foreground/background replay and approval transitions.
- Scheduler jobs now use a non-blocking filesystem lock to prevent duplicate execution across scheduler instances on Unix hosts.
- Slice 1 scheduler hardening is complete for the current local-runtime scope; stronger distributed locking and platform-specific cancellation remain future work.
- Slice 2 has started: high-risk tool results are stored in a per-run execution ledger keyed by request fingerprint, preventing duplicate approved side effects.
- Service-level tool requests now fail closed for high-risk actions without run context and delegate contextual checks to the kernel policy evaluator.
- Service lifecycle events now persist normalized policy decisions and policy context for denied, approval-pending, started, and completed requests.
- Workflow entry points now persist normalized policy context into run records and workflow-created events for replay continuity.
- The public API `run_workflow` path now preserves policy context in the same way as `start_run` and queued execution.
- Scheduled job payloads now normalize and persist policy context at creation.
- High-risk execution ledger behavior is covered across duplicate requests and service restart replay.
- The remaining Slice 2 work is broader retry integration and policy normalization for any remaining external adapters.
- Slice 3 has started: repository tasks accept explicit workspace-scoped verification commands with validation errors preserved through post-patch and retry flow.
- Repository inspection now enforces file, directory, depth, and byte limits and reports inspection usage in the workspace summary.
- Bounded UTF-8 previews for inspected text files are now included in workspace summaries; binary/unreadable files are skipped.
- Corrective patch proposals now pass through the same file-count and byte-budget checks as initial proposals.
- Task checkpoints now persist workspace identity and file hashes, reject external workspace changes on resume, and ignore Hermes-managed database/checkpoint artifacts.
- Checkpoint resume now rejects task and policy-context identity changes with explicit `TASK_CONFLICT` and `POLICY_CONFLICT` results.
- Policy identity hashing now excludes non-policy task controls, preventing false resume conflicts when iteration or budget limits change.
- Verification outcomes now map to canonical `COMPLETED_VERIFIED`, `COMPLETED_UNVERIFIED`, and `FAILED_VERIFICATION` task statuses while preserving the legacy response status.
- Explicit verification commands now require an approved executable (`python`, `pytest`, `make`, or `npm`) in addition to workspace scoping.
- Path-like operands for allowed Python, pytest, Make, and npm verification commands are now checked against the workspace boundary.
- Verification recovery now honors `max_verification_attempts` instead of retrying unconditionally.
- Public `run_task` responses now expose canonical `task_status` and `status_schema_version` fields, including resume-conflict responses, while retaining legacy `status`.
- Scheduler execution snapshots and returned job records now preserve canonical `task_status` and `status_schema_version` from task executors.
- Repository tasks now enforce `max_wall_time_seconds` before provider generation or patch mutation when the budget is exhausted.
- Verification stdout/stderr now obey `max_output_bytes`; oversized verifier output becomes `verification_output_exceeded` with truncated diagnostics.
- Budget-limited tasks now map to canonical `PARTIAL` status rather than being reported as unverified completion.
- Verification attempts now use the task wall-time budget, bounded by the existing 30-second ceiling.
- New task checkpoints persist schema version 1; legacy missing-version checkpoints remain readable, while unsupported future versions return `CHECKPOINT_CONFLICT`.
- Checkpoints now also persist task and policy-context identity hashes, rejecting changed task or policy inputs while allowing inherited checkpoint policy context.

## Current capability assessment

Hermes currently provides:

- Event-sourced workflow execution with persisted replay.
- Workflow and tool approval flows, including destructive tool approval.
- Tool contracts, risk classification, policy evaluation, and policy-denial records.
- Filesystem, shell, and Git tool runtimes with allowlists and bounded execution.
- Checkpoints, resume, rollback, patch diffs, before/after hashes, and change records.
- Multi-file staged patch application with rollback on injected staging and replacement failures.
- A bounded repository inspection and planning summary.
- Verification discovery and structured verification reports.
- One bounded corrective patch path.
- Bounded, sanitized, relevance-ranked memory retrieval with recorded influence.
- Provider routing and fallback behavior.
- A SQLite-backed scheduler and CLI/API surfaces.
- A broad regression suite is currently green with 205 passing tests.

The main limitation is not missing isolated features. It is inconsistent composition across entry points and an execution contract that is still distributed across the runtime service, kernel, tools, CLI, and API layers.

## Plan audit corrections

The following constraints are part of this plan rather than optional details:

- The kernel workflow state machine and the repository `run_task` loop currently use different status vocabularies. The new task status model must be introduced through an adapter and compatibility mapping; it must not silently rename existing persisted workflow states.
- Scheduler jobs currently persist `runtime_budget_seconds`, but the generic scheduler does not enforce that budget around the executor. Budget enforcement is implementation work, not an existing guarantee.
- Approval persistence does not by itself guarantee idempotent side effects. A request fingerprint and execution ledger are required before claiming that an approved destructive request executes at most once.
- Verification status changes are externally visible behavior. Existing `COMPLETED` responses and persisted records need a versioned compatibility strategy before introducing `COMPLETED_VERIFIED` and related states.
- OS resource limits are not a complete sandbox. Filesystem and network isolation require separate platform capabilities and explicit fallback behavior.
- Generated build artifacts are part of the current worktree but are not a reliable source baseline. Source/build cleanup is a prerequisite for a reviewable release, not a cosmetic follow-up.

### Likely failure points and mitigations

- Risk: the repository-task planner drifts into an autonomous agent and starts mutating files without an explicit proposal. Mitigation: keep the planner read-only and require the runtime service to accept and validate patch proposals before applying changes.
- Risk: sandbox checks become scattered and bypassable across entry points. Mitigation: centralize enforcement through a shared capability contract in the policy layer and consume it from the tool runtime and service layer.
- Risk: retries and fallback behavior silently consume the task budget. Mitigation: route retries through the same budget and stop-reason model used by the primary execution loop.
- Risk: checkpoint and resume logic becomes schema-fragile as the runtime evolves. Mitigation: version checkpoints, preserve compatibility for old payloads, and require explicit conflict handling for unsupported versions.
- Risk: memory starts overriding policy, verification, or budget decisions. Mitigation: keep memory advisory-only, record influence in summaries, and ensure policy and budget enforcement remain authoritative.
- Risk: distributed locking becomes platform-dependent and brittle. Mitigation: define a backend interface first, keep a local file-based backend as the default, and add lease expiry and recovery tests early.

## ROI priorities

### Priority 1: Canonical task execution contract

Value: exceptional
Cost: medium

Create one explicit task state model for repository tasks and a status adapter for workflow executions, CLI, API, scheduled jobs, and recovery. Do not force the existing workflow event state machine and repository-task state machine into one persisted enum until compatibility behavior is tested.

Required states:

- `QUEUED`
- `PLANNING`
- `WAITING_APPROVAL`
- `APPLYING`
- `VERIFYING`
- `CORRECTING`
- `COMPLETED_VERIFIED`
- `COMPLETED_UNVERIFIED`
- `FAILED_VERIFICATION`
- `BLOCKED_POLICY`
- `PARTIAL`
- `CANCELLED`

Every transition should record:

- task ID and checkpoint ID
- current iteration
- policy decision
- budget usage
- patch/change ID
- verification result
- stop reason

Do not add more autonomous behaviors until this contract is authoritative for repository tasks and its mapping to existing workflow/run statuses is explicit.

### Priority 2: Unified policy gate

Value: exceptional
Cost: medium

Route every tool attempt through one policy evaluation helper, including:

- workflow tool requests
- `RuntimeService.execute_tool_request`
- retries
- approval continuations
- replayed requests
- scheduled jobs
- CLI and API calls

The helper must normalize and validate:

- policy context
- tool identity and action
- risk level
- step constraints
- workspace constraints
- approval state
- retry/replay identity

Missing policy context must deny sensitive execution. Every decision must be persisted in lifecycle events and exposed in summaries.

Acceptance criteria:

- A denied request cannot reach a tool runtime through any public entry point.
- An approved destructive request executes at most once for a request fingerprint, enforced by a persisted execution ledger rather than approval state alone.
- Replay and resume preserve the original policy decision context.
- Tests cover direct service execution, workflow execution, approval, retry, replay, CLI, and API paths.

### Priority 3: Explicit task budgets

Value: very high
Cost: low-medium

Replace scattered limits with a persisted task budget object for repository tasks. Integrate it with, rather than duplicate, the scheduler's existing `runtime_budget_seconds` field.

Initial budget fields:

- `max_iterations`
- `max_corrections`
- `max_tool_calls`
- `max_patch_files`
- `max_patch_bytes`
- `max_verification_attempts`
- `max_wall_time_seconds`
- `max_model_tokens`
- `max_output_bytes`

Track consumption in checkpoints, lifecycle events, scheduler job payloads, and summaries. Enforce wall time around the executor, not only as stored metadata. Reaching a budget must stop the task with a specific reason rather than silently changing behavior.

### Priority 4: Verification-driven completion

Value: exceptional
Cost: low-medium

Make verification authoritative across every surface.

Rules for the new task contract:

- No verification command means `COMPLETED_UNVERIFIED`, never verified success. Introduce this through a response/checkpoint schema version or compatibility field so existing consumers of `COMPLETED` do not break silently.
- A failed verification result cannot be reported as ordinary completion.
- Correction is bounded to one attempt by default.
- Verification output, command, exit code, attempts, and retry reason are persisted.
- CLI, API, workflow, and scheduled job statuses use the same vocabulary.

Add a verification adapter interface for pytest, Make, package scripts, and explicitly configured task commands. Explicit commands must be allowlisted, workspace-scoped, bounded by the task budget, and approval-gated when their risk requires it.

### Priority 5: Bounded repository task contract

Value: very high
Cost: medium

Make the narrow repository workflow a first-class task type:

1. Inspect workspace within file, depth, and byte limits.
2. Select candidate files using task terms and repository metadata.
3. Read only selected files within limits.
4. Build a structured plan.
5. Produce a patch proposal.
6. Preview the diff.
7. Apply only after policy/review checks.
8. Verify.
9. Correct at most once.
10. Stop with a structured summary.

The planner must remain read-only. No implicit file mutation should occur without an explicit or provider-generated proposal.

Acceptance criteria:

- A README or small Python maintenance task can complete end to end.
- A patch cannot exceed file/count/byte limits.
- The task never scans or edits outside the workspace.
- Failed verification produces a correction or a clear blocked result.
- The final summary includes plan, selected files, diff, verification, and stop reason.

### Priority 6: Resume and workspace conflict protection

Value: high
Cost: medium

Add resume compatibility checks using:

- task hash
- workspace path and identity
- policy-context hash
- checkpoint schema version
- patch/change ID
- hashes of files touched by the previous iteration

If the workspace changed externally, resume must stop with `WORKSPACE_CONFLICT` rather than applying a stale patch.

Add process-restart integration tests covering pending approval, completed patch, failed verification, rollback, and an externally modified workspace. Store a workspace identity and touched-file hashes in the checkpoint before allowing resume or rollback.

## Secondary work after the core contract

### Process containment and sandboxing

Keep the existing command, cwd, environment, output, timeout, CPU, and memory controls. Then add, in order:

1. Process-group cleanup on timeout and cancellation.
2. Environment sanitization and explicit inheritance rules.
3. Network deny/allow policy.
4. Filesystem namespace isolation.
5. Privilege dropping and platform-specific sandbox backends.

Do not present application-level allowed roots as a complete security boundary.

### Provider health and routing

Add provider health, timeout classification, token/cost reporting, and fallback summaries after the task contract is stable. Prefer reliable fallback behavior over complex model selection.

### Memory feedback

Keep memory bounded and advisory. Add provenance, confidence, expiration, and outcome feedback. Memory must never override policy, approval, budget, or verification decisions.

### Scheduler hardening

Add enforced job budgets, concurrency/workspace locks, stale checkpoint handling, approval behavior for scheduled changes, and persistent failure notifications. A scheduler result must distinguish executor completion from verified task completion.

## Architecture-aware implementation strategy

The current architecture already has the right structural seams for this next phase, but they are not yet fully unified. The system is split across a few layers that each own part of execution:

- [engine/runtime_service.py](engine/runtime_service.py) is the public orchestration layer. It owns repository-task execution, budgets, checkpoints, verification, workspace inspection, task summaries, and the public response shape.
- [engine/runtime.py](engine/runtime.py) is the workflow kernel. It manages workflow and step state, event replay, approval transitions, tool-request lifecycle, and state reconstruction after restart.
- [engine/tool_runtime.py](engine/tool_runtime.py) is the execution boundary for filesystem and shell work. It already contains the strongest local guardrails, but these need to become part of a formal capability/sandbox model rather than ad hoc checks.
- [engine/scheduler.py](engine/scheduler.py) owns background execution, job budgets, and lock coordination. It is the natural place to enforce cross-instance safety and operational observability.
- [engine/models.py](engine/models.py) holds the shared contracts such as execution context, artifact records, tool requests, and workflow state. This file should become the canonical home for task-specific envelopes and execution metadata rather than leaving those concepts scattered across the service layer.
- [engine/policy.py](engine/policy.py) is already the policy decision point for tool actions. It should be extended to cover sandbox capability checks, network policy, and task-level execution constraints rather than remaining a narrow tool-policy evaluator.

That means the most important design principle for the next phase is: keep the kernel responsible for workflow state transitions and replay, while the service layer should own the repository-task loop, verification semantics, budgets, and public summaries. The kernel should not absorb all repository-task semantics directly; instead it should receive explicit execution intents and policy context from the service layer.

### Architectural changes to make first

1. Introduce a unified task envelope
   - Add a repository-task execution envelope in [engine/models.py](engine/models.py) that captures the task spec, task identity, policy context, budget, workspace identity, verification expectations, and stop reason.
   - Make the runtime service produce and consume this envelope consistently across API, CLI, workflow, and scheduler paths.
   - Persist the envelope in checkpoints and summaries so resume behavior is deterministic and auditable.

2. Separate “workflow state” from “task outcome”
   - Keep the workflow state machine in [engine/runtime.py](engine/runtime.py) for step/approval/replay semantics.
   - Layer repository-task outcomes on top of that state with a separate task status contract. The task contract should be explicit and compatibility-friendly rather than forcing the workflow state machine to carry all repository-task semantics directly.
   - This keeps the runtime understandable while avoiding a brittle merge of two state models.

3. Move sandboxing into a capability model
   - Extend [engine/models.py](engine/models.py) and [engine/policy.py](engine/policy.py) with a capability contract that describes allowed commands, allowed roots, allowed environment keys, network access, writable paths, and privilege expectations.
   - Use that capability contract from [engine/tool_runtime.py](engine/tool_runtime.py) and [engine/runtime_service.py](engine/runtime_service.py) rather than letting each layer infer behavior independently.

4. Add provider-health awareness to the existing router
   - The current provider routing in [engine/runtime_service.py](engine/runtime_service.py) and [workers/model_adapter.py](workers/model_adapter.py) is simple and local. It should evolve into a health-aware routing layer with explicit outcomes for timeouts, retries, cost, and fallback.
   - This should preserve the existing adapter interface but add a registry of provider state and route selection metadata.

5. Expand memory into a feedback-aware advisory layer
   - The memory store in [engine/runtime_service.py](engine/runtime_service.py) is currently bounded and useful but simple. It should evolve into a structured advisory memory system with provenance, confidence, expiration, and outcome fields without ever overriding policy or verification decisions.

6. Make the scheduler lock layer explicit and pluggable
   - The current lock approach in [engine/scheduler.py](engine/scheduler.py) is a good foundation, but it should be formalized behind a lock backend contract with lease/renewal semantics and stale-lock recovery.
   - That change will make the system safer when multiple scheduler processes or workers race to execute the same job.

### Recommended implementation sequence

#### Phase 1: stabilize the repository-task contract

Focus: [engine/runtime_service.py](engine/runtime_service.py), [engine/models.py](engine/models.py), [engine/runtime.py](engine/runtime.py)

- Introduce a structured repository-task spec and a task execution envelope.
- Persist the plan artifact, selected files, verification expectations, patch preview, and stop reason in checkpoints and summaries.
- Ensure one task status model is exposed consistently to API, CLI, scheduler, and workflow surfaces.
- Keep the workflow engine state machine intact, but make task outcome mapping explicit and versioned.

Why this comes first: all later workstreams depend on a stable task contract. Without it, provider routing, sandbox decisions, memory, and distributed locks will all be harder to reason about.

#### Phase 2: add capability-driven sandboxing

Focus: [engine/models.py](engine/models.py), [engine/policy.py](engine/policy.py), [engine/tool_runtime.py](engine/tool_runtime.py), [engine/runtime_service.py](engine/runtime_service.py)

- Define a capability contract in the execution context and policy evaluation path.
- enforce command allowlists, allowed roots, environment inheritance, output caps, and network-policy decisions through that contract.
- Record sandbox decisions in execution events and summaries so denials and fallbacks are visible.
- Keep fail-closed semantics for any unsupported or ambiguous capability state.

Why this comes second: sandboxing is the main safety boundary. It needs to sit on top of the task contract so that policy decisions remain consistent across planning, execution, retries, and resume.

#### Phase 3: make provider routing observable and resilient

Focus: [engine/runtime_service.py](engine/runtime_service.py), [workers/model_adapter.py](workers/model_adapter.py), [engine/models.py](engine/models.py)

- Add provider health tracking, timeout classification, retry classification, and cost/token reporting.
- Update the routing logic so that fallback decisions are explicit rather than implicit in the adapter selection.
- Persist route and fallback metadata in task summaries and checkpoints.

Why this comes third: once the task loop is stable and safe, provider behavior becomes the next major source of runtime variability and should be made observable.

#### Phase 4: upgrade memory into a feedback-aware advisory system

Focus: [engine/runtime_service.py](engine/runtime_service.py)

- Extend the memory schema with provenance, confidence, expiration, and outcome feedback.
- Rank memories using both relevance and recorded usefulness.
- Keep memory advisory-only and ensure it cannot override policy, verification, or budget enforcement.

Why this comes fourth: memory should improve the loop, but only after the core execution contract is stable and safe.

#### Phase 5: harden distributed execution

Focus: [engine/scheduler.py](engine/scheduler.py), [engine/storage.py](engine/storage.py), [engine/runtime_service.py](engine/runtime_service.py)

- Introduce a lease-based lock backend and stale-lock recovery.
- Ensure that lock ownership and task checkpoint identity are checked before starting or resuming a job.
- Persist lock contention and recovery events so operators can diagnose race conditions.

Why this comes last: distributed safety is most valuable once the local execution loop and storage contracts are already reliable.

### What success should look like

This phase is successful when:

- a repository task can complete end to end from inspection to verification with a bounded and understandable loop;
- tool and task execution are guarded by explicit capabilities and sandbox policy;
- provider failures produce observable routing and fallback behavior;
- memory improves recommendations without bypassing safety rules;
- scheduler and worker execution remain safe across restarts and multiple processes.

### Suggested implementation order for the remaining workstreams

1. Repository-task automation loop and its tests.
2. Capability-based sandbox model and first network/policy enforcement hooks.
3. Provider health and routing with fallback and telemetry.
4. Memory feedback quality and lifecycle management.
5. Distributed execution hardening with lease-based locks and stale-lock recovery.

## Remaining workstreams implementation plan

The next implementation cycle should treat the remaining gaps as five coordinated workstreams rather than isolated enhancements.

### Workstream A: General repository-task automation

Goal: move from bounded repository-task support to a reusable task loop that can handle a broader class of maintenance work without becoming unsafe or unbounded.

Implementation phases:

1. Task specification and planner contract
   - Define a structured repository-task spec with inputs, constraints, expected files, verification commands, and stop conditions.
   - Add a dedicated planner adapter that returns a plan artifact, candidate files, and a patch proposal contract instead of ad hoc text output.
   - Persist the plan artifact in checkpoints and summaries so resume and replay are deterministic.

2. Execution loop hardening
   - Introduce a first-class execution loop for repository tasks: inspect → plan → patch proposal → preview → apply → verify → correct once → stop.
   - Make each phase explicit in task transitions and summaries so the runtime can report why it stopped.
   - Bound every phase by the existing task budget model and preserve the last good checkpoint before a correction attempt.

3. Broader patch and verification coverage
   - Support multi-file patches, rename/move operations, and dependency-aware verification commands.
   - Add verification adapter support for pytest, make, npm, and explicit task-configured commands with clear failure classification.
   - Ensure correction attempts are limited, recorded, and surfaced in public responses.

4. End-to-end scenario coverage
   - Add a small but representative end-to-end test for a real maintenance task such as README cleanup, minor Python refactor, or a small config change.
   - Validate success, verification failure, and correction paths with real filesystem and subprocess behavior.

Acceptance criteria:
   - A repository task can complete end to end from inspection through verification without manual intervention.
   - The task stops cleanly with a structured reason when a budget, policy, or verification gate is hit.
   - The final summary includes the plan, selected files, patch preview, verification result, and stop reason.

### Workstream B: Sandboxing and isolation

Implementation note: do not treat the current file, command, and environment guardrails as a complete sandbox. The first milestone should be a capability contract and explicit fail-closed behavior; network and namespace isolation should be introduced only after that contract is in place and tested.

Goal: make the runtime safer by turning current guardrails into a real capability model rather than a set of local checks.

Implementation phases:

1. Capability-based execution policy
   - Introduce a runtime capability contract for each execution context: allowed commands, allowed roots, allowed env vars, network access, and writable paths.
   - Make capabilities explicit in tool requests and execution envelopes rather than relying on implicit metadata.

2. Process and filesystem isolation
   - Enforce subprocess execution inside a controlled working directory and a process group with cleanup on timeout or cancellation.
   - Add a pluggable isolation backend interface that can use platform-native isolation when available and degrade safely when not.

3. Network policy
   - Add a deny-by-default network policy with explicit allow rules for approved external actions.
   - Record network-policy decisions in tool execution events and summaries so denials are visible and replay-safe.

4. Privilege handling
   - Add an explicit privilege model for privileged actions and fail closed when the runtime cannot guarantee safe execution.
   - Distinguish between local execution, delegated execution, and unsupported isolation modes.

Acceptance criteria:
   - Unsafe commands, outside-root file access, and unapproved network actions are denied before execution.
   - The runtime reports whether isolation was enforced, skipped, or unsupported for each execution.
   - The sandbox policy is enforced across workflow, API, CLI, and scheduler entry points.

### Workstream C: Provider health and routing

Implementation note: the existing provider router should remain deterministic and adapter-compatible; do not replace it with a complex model-selector system before the task contract and budgets are stable.

Goal: make provider selection deterministic, observable, and resilient under failure.

Implementation phases:

1. Provider registry and health model
   - Add a provider registry with health state, latency, timeout classification, and last failure metadata.
   - Track provider health per endpoint and per task class so routing decisions can be made from observed runtime data.

2. Routing and fallback policy
   - Implement routing rules based on provider health, task complexity, timeout classification, and cost budget.
   - Add failover behavior that preserves the reason for fallback and its impact on task progress.

3. Token and cost reporting
   - Persist token usage, estimated cost, and retry counts in task summaries and checkpoints.
   - Expose these values in scheduler and API responses so they are visible without requiring a separate log walk.

4. Retry classification
   - Define retryable vs non-retryable provider failures and make the runtime stop or escalate appropriately.
   - Ensure retry logic respects the task budget and verification constraints.

Acceptance criteria:
   - A failing provider causes a visible fallback with a structured reason.
   - Task summaries report health, route selection, retries, and cost metadata.
   - The runtime does not exceed the task budget when retrying provider failures.

### Workstream D: Memory feedback quality

Goal: make memory useful without allowing it to override policy, verification, or budget decisions.

Implementation phases:

1. Memory record schema expansion
   - Add fields for provenance, confidence, expiration, outcome, source task type, and last used timestamp.
   - Store whether a memory entry was helpful, neutral, or harmful after a task completes.

2. Feedback-driven ranking
   - Update memory scoring to account for relevance, recency, confidence, and outcome feedback rather than raw overlap alone.
   - Keep memory advisory-only; never let it bypass policy, verification, or budget checks.

3. Memory lifecycle management
   - Add expiration and pruning for stale entries so the memory store remains bounded and predictable.
   - Record memory influence in task summaries and checkpoints for auditability.

Acceptance criteria:
   - Memory improves next-step suggestions without affecting approval, verification, or patching decisions.
   - Memory entries carry provenance and outcome feedback that can be inspected later.
   - The memory store remains bounded and stable under repeated task execution.

### Workstream E: Distributed runtime hardening

Implementation note: start with local file-based leases and explicit ownership metadata. Avoid introducing a distributed backend before the lease contract and stale-lock recovery behavior are proven locally.

Goal: make the runtime safe to run across multiple scheduler or worker processes without duplicate execution or stale state.

Implementation phases:

1. Distributed lock contract
   - Introduce a lock backend interface with lease acquisition, lease renewal, and release semantics.
   - Support local file-based locks first, then add a pluggable backend for future distributed deployments.

2. Stale lock and heartbeat handling
   - Add lock ownership metadata, heartbeat renewal, and stale-lock cleanup so crashed workers do not block the system indefinitely.
   - Ensure a lock can be safely reclaimed when ownership expires.

3. Cross-instance execution safety
   - Make scheduler and worker execution paths verify lock ownership before starting a job and after resuming a checkpoint.
   - Ensure duplicate execution is prevented even when two processes race to start the same job.

4. Operational observability
   - Persist lock acquisition, release, and contention events so operators can inspect runtime contention and recovery behavior.

Acceptance criteria:
   - Two scheduler instances cannot execute the same job concurrently.
   - A crashed or stale worker cannot permanently block a new execution.
   - Lock state and contention are visible in runtime summaries and logs.

## Workstream implementation order

1. Complete the repository-task automation loop and its tests.
2. Add the capability-based sandbox model and the first network/policy enforcement hooks.
3. Add provider health and routing with fallback and telemetry.
4. Expand memory feedback quality and lifecycle management.
5. Harden distributed execution with lease-based locks and stale-lock recovery.

## Definition of done for the remaining workstreams

This phase is complete when:

- A repository task can move from inspection to patching to verification and correction with bounded, explainable behavior.
- Tool execution is guarded by an explicit capability and sandbox policy across all entry points.
- Provider routing is observable, health-aware, and resilient to transient failures.
- Memory is advisory, bounded, and feedback-informed rather than being a blind relevance store.
- Scheduler and worker execution are safe across multiple processes and recover cleanly from stale locks or restarts.
- The regression suite covers end-to-end success, failure, recovery, approval, and distributed-execution scenarios.

## Test strategy

Add public-surface scenario tests for:

- inspect-only task
- dry-run patch
- approved destructive tool request
- denied destructive tool request
- multi-file patch with successful verification
- staging failure rollback
- replacement failure rollback
- failed verification with one correction
- process restart and resume
- replayed approval request
- strict policy denial
- scheduled task budget exhaustion
- duplicate approved request with the same request fingerprint

Keep unit tests for policy normalization, budget accounting, patch validation, and status transitions. Use real tool runtimes for behavior tests and avoid mock-only assertions for safety guarantees.

## Slice implementation order

### Slice 0: Source and status baseline

- Separate generated build/dist artifacts from source changes.
- Document the supported test, build, and smoke-test commands.
- Define a response/checkpoint schema version and compatibility mapping for existing status values.
- Add a migration test for old persisted `COMPLETED` records.

### Slice 1: Execution contract and budgets

- Add task state and transition models.
- Add a persisted task budget.
- Normalize result statuses.
- Add transition and budget tests.
- Enforce scheduler wall-time budgets around job executors.

### Slice 2: Unified policy enforcement

- Extract the shared policy gate.
- Route service-level tool execution through it.
- Persist decisions and request fingerprints.
- Add a durable execution ledger so approval replay and duplicate requests cannot repeat side effects.
- Add approval, retry, replay, CLI, and API integration tests.

### Slice 3: Verification and repository task contract

- Add bounded inspection/read tools.
- Add verification adapters.
- Make success status verification-driven.
- Add the end-to-end small repository task scenario.

### Slice 4: Resume conflict protection

- Add workspace and task identity hashes.
- Reject incompatible resumes.
- Add restart and stale-checkpoint scenarios.

### Slice 5: Process containment

- Add process groups and cancellation cleanup.
- Add network/filesystem sandbox design behind explicit platform capabilities.

## Definition of done

This implementation cycle is complete when:

- All public repository-task execution paths expose one task state, policy, budget, and status contract, with an explicit compatibility adapter for existing workflow states.
- Sensitive tool execution is fail-closed and approval state is replay-safe.
- A narrow repository task can inspect, patch, verify, correct once, checkpoint, and resume.
- Verification status is never overstated.
- External workspace changes are detected before resume or rollback.
- Tool and task budgets are visible and enforced.
- Scheduler runtime budgets are enforced rather than only persisted as metadata.
- End-to-end scenario tests cover the major success, denial, failure, and recovery paths.
- Duplicate approved request fingerprints cannot repeat a side effect.
- The source/build baseline is reviewable and reproducible.
- The full regression suite remains green.

## Explicitly defer

Do not prioritize these in this cycle:

- Broad autonomous debugging.
- Long-horizon plans with many retries.
- Multi-repository orchestration.
- Arbitrary package installation.
- Unrestricted browsing or external automation.
- Complex multi-agent delegation.
- Embeddings-based memory.
- Autonomous Git push or deployment actions.
