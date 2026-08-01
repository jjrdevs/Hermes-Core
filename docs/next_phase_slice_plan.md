# Next Phase Slice Plan

Date: 2026-07-31

## Goal

Advance Hermes from a bounded, safety-oriented execution kernel to a more capable repository-task loop while preserving approval, rollback, and verification guarantees.

## Scope

This slice should focus on four concrete capabilities:

1. Broader fail-closed policy enforcement
2. Durable multi-file patch transactions and richer change records
3. A bounded repository inspection and corrective planning loop
4. Memory-informed execution decisions

## 1. Fail-closed policy enforcement

### Objectives
- Apply the same policy checks to all tool execution branches, including retries, approvals, and replayed requests.
- Ensure missing policy metadata causes a denial rather than an implicit pass-through.
- Make tool execution fail closed when policy context is incomplete.

### Deliverables
- A shared helper that normalizes tool policy context for every execution path.
- Tests proving that missing metadata or incomplete constraints block execution.
- Runtime summaries that describe the policy reason when a request is denied.

## 2. Durable multi-file patch transactions

### Objectives
- Replace the current lightweight patch handling with a more durable, auditable multi-file change workflow.
- Persist before/after hashes and rollback metadata for every file touched.
- Ensure a failed transaction leaves the workspace unchanged.

### Deliverables
- A transaction-style patch application path for multi-file writes.
- Atomic file replacement semantics with rollback metadata stored alongside the change record.
- Tests for partial-failure rollback and workspace integrity preservation.

## 3. Bounded repository inspection and corrective planning

### Objectives
- Move beyond the fixed notes-file pattern toward a small repository task loop.
- Inspect the repository, select relevant files, build a structured plan, and propose a constrained patch.
- Use verification output to drive at most one bounded correction round.

### Deliverables
- A repository task contract with workspace inspection, file selection, patch proposal, verification, and correction steps.
- A narrow task class for small repository edits.
- Tests showing that a real repository task can preview, apply, verify, and correct a patch without overstepping policy.

## 4. Memory-informed execution decisions

### Objectives
- Allow retrieved memory to influence planning and retry behavior.
- Rank memories by task relevance, recency, and keyword overlap.
- Keep memory sanitization and retention bounded.

### Deliverables
- A memory selection path that surfaces the best-ranked memories for a task.
- Runtime summaries that include the selected memories and their influence.
- Tests proving that memory retrieval is bounded and safe.

## Implementation plan for the unfinished work

### Phase 1 — Finish fail-closed policy propagation
- Introduce a shared policy-normalization path that is used before any tool request, retry, replay, or approval continuation.
- Make missing or incomplete policy metadata block execution with a concrete reason such as policy_context.missing or policy_context.incomplete.
- Surface the resolved policy state in task summaries so the runtime explains why a branch was denied.

### Phase 2 — Strengthen multi-file patch transactions
- Move repository patch application behind a small transaction helper that snapshots each file before change.
- Persist rollback metadata and before/after hashes alongside the checkpoint summary for every touched path.
- Ensure any failed write or validation step aborts before any file is left in a partial state.

### Phase 3 — Build a bounded repository task loop
- Add a narrow repository-task flow that performs: inspect workspace, select candidate files, build a repository plan, propose a scoped patch, verify, and apply at most one corrective patch.
- Keep the loop bounded with a hard iteration budget and a single correction round to avoid runaway behavior.
- Record the plan, verification outcome, and correction history in the task summary.

### Phase 4 — Make memory influence execution safely
- Retrieve a small set of memories, rank them by task relevance and recency, and include the top results in runtime summaries.
- Use the selected memories to guide next-step suggestions and retry strategy, while never overriding hard safety or policy decisions.
- Keep memory retention and sanitization bounded so the runtime remains predictable.

## Exit Criteria

## Implementation status

- Policy context normalization and strict fail-closed task execution are implemented, including policy continuity across checkpoint resume.
- Multi-file patch transactions now validate targets, stage replacements, preserve before/after hashes, and expose rollback metadata.
- Repository inspection and bounded planning are implemented; planning remains read-only unless a patch is explicitly or provider-generated.
- Memory retrieval is bounded, sanitized, ranked by relevance and recency, and contributes a recorded next-action strategy.
- Added a first OS-level sandbox layer for shell children through bounded CPU and address-space resource limits on Unix hosts.
- Remaining sandbox work: stronger filesystem and network isolation, which requires a dedicated platform-specific sandbox design.

## Exit Criteria

The next slice is complete when:
- policy enforcement is consistent and fail-closed across task, approval, retry, and replay paths;
- multi-file patch transactions preserve workspace integrity and leave audit trails under injected write failures;
- a narrow repository task can inspect, plan, patch, verify, and correct itself within bounds;
- memory retrieval meaningfully influences execution decisions without destabilizing the runtime;
- the regression suite remains green.
