# First-Slice Audit and Next Development Phase

Date: 2026-07-31

## Audit Result

The first slice is complete for the bounded local-first orchestration contract. It is not complete as a general autonomous coding system. The distinction matters because several capabilities below are prototypes or operator-facing summaries rather than full coding behavior.

- Workflow definitions and runtime executions remain separate.
- Workflow state, lifecycle changes, approvals, and tool events are persisted and replayable.
- Artifacts are persisted with hashes and lineage metadata.
- Workers and model adapters are selected through Hermes-owned contracts.
- The task execution path records plan, verification, retry, checkpoint, resume, memory context, provider routing, and structured summaries; its current edit and verification behavior remains intentionally narrow and is addressed in the next phase.
- CLI status and checkpoint commands expose operator-relevant progress.
- `run --dry-run` previews workflow steps without executing them.
- The demonstration task edit can be reverted with `rollback <checkpoint_id>`.
- The direct filesystem tool is root-bounded when configured with allowed roots, and the default runtime shell path uses an explicit command allowlist.
- Verification falls back to `not_run` instead of claiming success when no check is discoverable.

Validation evidence:

```text
pytest -q
157 passed in 17.27s
```

## Explicit Boundary

The first slice disables network tools by default in the configured runtime path: the default shell allowlist contains only local verification commands, and no network action is exposed through the workflow tool definitions. This is tool-level policy, not a kernel sandbox. Python remains an allowlisted interpreter, so arbitrary Python code is not equivalent to OS-level network isolation. The current host does not permit `unshare -n`, so Hermes must not claim complete OS-level network isolation until a supported sandbox boundary is introduced.

The first slice also keeps task editing intentionally narrow. It demonstrates a reversible `notes.txt` patch rather than claiming to be a general autonomous coding system.

## Known Failure Points and Partial Guarantees

These are current limitations, not completed first-slice capabilities:

- Narrow single- and multi-file patch proposals, including strict JSON planner output, are normalized and validated before application; opt-in adapter generation now supplies proposals, while the no-proposal fallback still uses the fixed `notes.txt` demonstration edit and corrective iterations remain incomplete.
- Pre-change verification is recorded and non-dry-run edits are now verified after application; one bounded adapter-driven correction is supported, while broader corrective planning remains incomplete.
- Single- and multi-file proposed patches now carry stable change IDs, per-file hashes, and rollback metadata; checkpoint snapshots are written atomically, while event-log-backed change records and crash-safe multi-file filesystem transactions remain incomplete.
- Destructive tool requests now pause for action-level approval, and pending approvals plus policy denials are discoverable and controllable through the service, API, and CLI.
- The runtime shell path now restricts working directories, selected child environment keys, output size, and timeout ceilings; it still cannot provide OS-level isolation for arbitrary behavior inside an allowlisted interpreter.
- Memory retrieval is bounded keyword and metadata matching. Retrieved memories are included in summaries but do not yet reliably change planning or retry behavior.
- Provider routing uses static profiles and simple heuristics. It does not yet provide health checks, cost accounting, circuit breaking, or reliable escalation based on verification results.
- Background jobs are lightweight daemon-thread execution. Runtime budgets, durable scheduling, shutdown, and notification behavior are not yet production-grade.

## Current Capability Assessment

Hermes is now a credible orchestration and recovery kernel, but it is not yet a general autonomous coding assistant.

It has:

- durable workflow state, event replay, artifacts, checkpoints, and operator summaries;
- workflow-level approval, resume, cancellation, and background run controls;
- basic filesystem roots, shell command allowlists, timeouts, retries, dry-run previews, and rollback;
- bounded keyword memory, a minimal provider router, and a lightweight job scheduler.

It is still missing the central coding behavior:

- the local worker still produces artifacts and model text rather than repository patches, while the task service can now opt in to adapter-generated strict JSON proposals;
- `run_task` supports bounded single- and multi-file patch proposals, while retaining the fixed `notes.txt` fallback;
- non-dry-run patches are verified after application and retain the pre-change verification result;
- tool recommendations are not yet typed strongly enough to guarantee valid action parameters;
- action-level approval exists for destructive tool requests, with pending approval discovery, CLI approve/deny controls, and policy-denial inspection available; the broader operator surface is still incomplete.
- change records can be inspected through the service, API, and `change get`, including diffs, hashes, verification, and rollback state;
- patch proposals are normalized, recorded separately from applied changes, and malformed proposals fail before any write;
- strict JSON planner output can feed the proposal contract and non-JSON planner output fails closed;
- `generate_patch` can request a strict JSON proposal from the selected adapter while preserving dry-run and workspace validation;
- failed post-change verification can trigger at most the configured bounded correction count, with correction proposals validated through the same path;

The next phase must therefore address both safety and the real task loop. Safety without task-directed execution would only make the demonstration more controlled.

## Audit Update: Current Status and Remaining Work

As of 2026-07-31, the repository has moved well beyond the first-slice demo and now satisfies several of the current-phase safety requirements:

- tool actions resolve a risk classification and that classification is now propagated into tool execution envelopes and completed tool events;
- destructive requests pause for explicit approval and can be approved or denied through the runtime service;
- patch proposals, verification, checkpointing, rollback, and change-record inspection are implemented and covered by regression tests;
- the full suite remains green with `159 passed in 17.11s`.

The main gaps that remain before the current phase can be considered complete are:

- OS-level sandboxing is still not available; the current guarantee is a policy-aware tool executor rather than a true operating-system sandbox.
- Fail-closed policy defaults need to be enforced consistently across all tool paths and execution branches, not only the common happy path.
- Crash-safe multi-file filesystem transactions and event-log-backed change records remain unfinished.
- The task loop is still narrow. It can apply a bounded patch, verify it, and roll back, but it is not yet a broad multi-step repository-coding loop.
- Memory and provider-routing improvements are still deferred; they do not yet meaningfully influence planning or retry decisions.

The next implementation slice should therefore focus on: 1) broader fail-closed policy enforcement, 2) durable multi-file patch transactions and change-record persistence, 3) richer bounded repository inspection and corrective planning, and 4) memory-informed execution decisions.

## Next Phase: Safe, Task-Directed Execution

The next phase should make tool authorization risk-aware and auditable while replacing the fixed demonstration edit with a narrow, real repository task loop.

This phase is now effectively complete for the current scope: risk-aware approvals, policy-aware execution envelopes, verification/rollback/change-record support, and the relevant regression coverage are in place.

The next implementation slice is documented in [docs/next_phase_slice_plan.md](docs/next_phase_slice_plan.md).

### Implementation Progress

The first increment is implemented:

- tool definitions now expose one of `read_only`, `write_safe`, `destructive`, `privileged`, or `external_network`;
- legacy definitions without an explicit classification receive a conservative action-based classification;
- unsupported explicit classifications fail validation;
- tool request policy context and persisted request events include the resolved risk level;
- destructive, privileged, and external-network actions now pause before side effects and require approval for the exact request;
- tool approval grants are persisted as events and survive runtime restart;
- tool approval denials are persisted, fail the affected execution, and do not invoke the requested action;
- pending tool approvals are queryable through the service, API, and CLI with sensitive parameter keys redacted;
- pending tool approvals can be approved or denied through dedicated CLI commands with actor, reason, and comment metadata;
- persisted change records can be inspected through the service, API, and CLI without mutating workspace state;
- policy-denied actions can be inspected through the service, API, and `policy denials` without mutating runtime state;
- change records have stable IDs and checkpoint snapshots use atomic replacement to avoid partial JSON writes;
- the runtime shell path enforces bounded working directories, environment keys, output size, and timeout ceilings;
- bounded single- or multi-file `proposed_patch` values can be previewed without mutation, applied inside the workspace, hashed per file, and verified after application;
- rollback detects post-patch conflicts and records an auditable checkpoint rollback record with actor, reason, and hashes;
- multi-file rollback validates every current hash before restoring the change set;
- the full regression suite passes with `157` tests.

OS-level sandboxing, multi-step model-driven corrective planning, fail-closed policy defaults for all tool paths, event-log-backed change records, crash-safe multi-file filesystem transactions, and complete change-record controls remain planned work.

### 1. Risk-classified tool contracts

Add explicit risk metadata to every tool and action:

- `read_only`
- `write_safe`
- `destructive`
- `privileged`
- `external_network`

Persist the classification in tool definitions and include it in every execution envelope and event.

### 2. Policy-driven approval gates

Extend the existing approval transition so high-risk tool requests can pause independently of workflow transitions. The decision should include:

- tool and action
- requested parameters or a redacted parameter summary
- risk class
- actor or requester
- reason
- approval scope
- correlation id

Approval and denial must be replayable and deterministic after restart.

### 3. Stronger execution boundaries

Move the remaining enforcement into one policy-aware tool executor:

- allowlisted filesystem roots, including the explicit task workspace
- allowlisted shell commands and bounded working directories
- restricted child-process environment
- explicit network capability rather than implicit network access
- bounded timeouts, output sizes, and retry budgets
- fail-closed behavior for missing policy metadata

Where OS sandboxing is available, use it. Otherwise report the weaker tool-level guarantee in the run summary.

### 4. Recovery and rollback records

Replace the demonstration-only patch mechanism with a durable change record:

- before and after hashes
- affected paths
- reversible backup or patch artifact
- verification result associated with the change
- rollback status and actor

Rollback should refuse targets outside the recorded workspace and should emit an event rather than silently changing a file.

### 5. Operator/API surface

Expose the same controls through CLI and API:

- inspect pending approvals
- approve or deny a specific action
- inspect policy-denied tool requests
- preview a proposed action
- rollback a change record
- observe the exact stop reason and next action

### 6. Real repository task loop

Replace the fixed `notes.txt` behavior with a constrained execution sequence:

```text
task specification
-> workspace inspection
-> relevant file selection
-> structured plan
-> patch proposal
-> diff preview
-> policy and approval decision
-> patch application
-> post-change verification
-> retry, rollback, escalation, or completion
```

The first implementation should support a narrow patch format and a small repository task class. It should not attempt unrestricted autonomous coding yet. Each action must produce structured inputs and outputs so the next step can use actual tool results rather than static summary text.

### 7. Post-change verification

Move verification after patch application for non-dry-run tasks. Persist both the pre-change and post-change verification results, including:

- command or check used;
- exit status;
- captured output with size limits;
- retry count;
- files or change record being verified;
- final status of `passed`, `failed`, or `not_run`.

## Exit Criteria for the Next Phase

The next phase is complete when:

- every tool request has a risk classification;
- destructive or external actions pause for explicit approval;
- denied actions have no side effects;
- approval state survives restart and is visible through CLI/API queries;
- rollback is represented by an auditable event and verified against before/after hashes;
- a narrow repository task can produce a real patch, preview it, apply it under policy, and verify the resulting workspace;
- all relevant policy and recovery tests pass without weakening the existing `pytest -q` baseline.

## Future Development

These items should follow the safe task loop rather than precede it.

### Memory that changes behavior

- ingest reusable lessons from verification failures, successful approaches, tool output, and user notes;
- sanitize metadata as well as memory content;
- retrieve by project, task type, recency, and keyword before considering embeddings;
- use retrieved memory to influence planning and retry decisions;
- support pinning, deletion, summarization, and bounded retention.

### Reliable provider routing

- honor explicit provider preferences consistently;
- distinguish task complexity instead of routing every task as `simple`;
- add availability checks, health tracking, failover, and circuit breaking;
- record latency, token usage, cost class, and fallback frequency;
- escalate only when task requirements or verification results justify it.

### Durable unattended jobs

- enforce runtime budgets rather than only storing them;
- persist next-run scheduling and interruption state atomically;
- make cancellation and restart semantics deterministic;
- support concurrent-job limits and clean process shutdown;
- produce durable reports or notifications for partial and completed work.

### Stronger sandboxing and tool coverage

- restrict child-process environments and working directories;
- enforce output-size and resource limits;
- add explicit Git inspection and mutation policy;
- introduce an OS-level sandbox when the host supports one;
- keep network access as an explicit capability instead of an ambient process permission;
- add typed schemas for filesystem, shell, Git, verification, and artifact actions.

### Broader coding capability

- support multi-file patches and conflict detection;
- add repository diff and status inspection;
- support test, lint, and build checks selected from the task contract;
- add bounded corrective iterations driven by actual failure output;
- integrate a stronger patching backend only behind Hermes policy, checkpoint, and artifact boundaries.

### UI and integration surfaces

- expose run, observe, artifact, approval, denial, preview, rollback, and resume flows consistently through API and UI;
- show policy decisions and verification evidence, not just final status;
- preserve Hermes Core as the authority for runtime state, approvals, artifacts, and recovery.

The intended long-term direction is a narrow, trustworthy unattended coding worker: capable of making useful progress on specified repository tasks, transparent about uncertainty, reversible when changes fail, and deliberately bounded when a task exceeds its policy or verification evidence.
