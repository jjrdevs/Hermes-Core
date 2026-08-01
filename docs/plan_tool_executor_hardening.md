# Implementation Plan: Tool Executor Hardening

## Objective

Upgrade Hermes Core from a basic tool runtime into a reliable tool-execution substrate for autonomous work. This is the highest-ROI improvement because it immediately improves correctness, safety, and recoverability without requiring a large redesign.

## Why this matters

The current runtime already has a basic tool layer in [engine/tool_runtime.py](../engine/tool_runtime.py), but it is still thin. It can execute simple file and shell operations, yet it lacks the richer behaviors that make tool use dependable in a real agent loop:

- schema validation before execution
- retries for transient failures
- timeout handling for long-running commands
- structured output normalization
- guardrails for destructive or high-risk actions
- clear error classification for downstream policy loops

## Design goals

1. Keep Hermes Core as the execution authority.
2. Make tool execution deterministic and auditable.
3. Treat tool failures as first-class runtime events, not silent exceptions.
4. Provide enough structure that the planning loop can reason about tool outcomes.

## Proposed architecture

### 1. Tool contract layer

Introduce a formal tool contract object that every tool implements. The contract should define:

- tool name
- input schema
- output schema
- allowed actions or modes
- risk level
- retry policy
- timeout policy

This contract should live alongside the runtime model in [engine/models.py](../engine/models.py) and be consumed by the runtime service in [engine/runtime_service.py](../engine/runtime_service.py).

### 2. Tool execution wrapper

Add a wrapper around the existing tool runtime so every tool invocation passes through the same pipeline:

1. validate parameters
2. resolve tool instance
3. enforce sandbox and policy constraints
4. execute with timeout
5. normalize result into a structured envelope
6. emit an event or artifact update
7. classify success, retryable failure, or terminal failure

### 3. Retry and timeout policy

Implement policy-driven execution behavior:

- retries for transient network or process-level errors
- no retry for destructive actions unless explicitly permitted
- exponential backoff with a cap
- per-tool timeout defaults and override support
- cancellation support for long-running shell operations

### 4. Output parsing and normalization

Most tool failures are not caused by execution alone; they come from brittle downstream assumptions. Add a normalization step that converts raw tool results into:

- status: success, partial, failed, cancelled
- output payload
- stdout/stderr preview
- structured metadata
- error classification

This will make the planner and runtime easier to reason about.

## Implementation phases

### Phase 1 — Introduce a tool execution envelope

- add a new tool result model in [engine/models.py](../engine/models.py)
- add a wrapper in [engine/tool_runtime.py](../engine/tool_runtime.py)
- ensure all tool runtimes return a consistent envelope

Acceptance criteria:

- every tool invocation returns the same top-level structure
- failures are classified consistently
- callers can inspect tool outcomes without parsing ad hoc formats

### Phase 2 — Add schema validation

- define JSON-schema-style input validation for filesystem, shell, and git tools
- reject invalid requests before execution
- emit a structured validation error event

Acceptance criteria:

- malformed tool requests are rejected before side effects occur
- invalid input produces a deterministic error with context

### Phase 3 — Add retries, timeouts, and cancellation

- add retry policy support per tool class
- enforce timeout defaults and overrides
- support cancellation through run state and control actions

Acceptance criteria:

- transient failures are retried within policy limits
- long-running tasks do not hang indefinitely
- cancelled tasks produce a clear runtime state transition

### Phase 4 — Add guardrails and risk classification

- classify tools into read-only, write, destructive, and privileged categories
- enforce policy before execution
- require approval for high-risk actions such as shell commands with mutation semantics

Acceptance criteria:

- destructive operations are not executed without explicit policy clearance
- risk classification is visible in the runtime event stream

### Phase 5 — Add observability and structured events

- emit tool-start, tool-success, tool-failed, tool-retried, and tool-cancelled events
- include duration and error classification in the payload
- surface this through the existing run event contract

Acceptance criteria:

- the agent or UI can observe tool progress without scraping logs
- failures are diagnosable from the event stream alone

## Files to change

- [engine/models.py](../engine/models.py)
- [engine/tool_runtime.py](../engine/tool_runtime.py)
- [engine/runtime.py](../engine/runtime.py)
- [engine/runtime_service.py](../engine/runtime_service.py)
- [engine/api.py](../engine/api.py)
- [engine/http_adapter.py](../engine/http_adapter.py)

## Tests

Add targeted tests for:

- valid tool request execution
- invalid tool parameter rejection
- retry-on-failure behavior
- timeout handling
- destructive-action guardrail enforcement
- event emission for tool lifecycle transitions

## Estimated effort

- 2 to 4 weeks for a solid first version
- 4 to 6 weeks for a robust production-grade version

## Definition of done

This work is complete when the system can execute tools predictably, safely, and with meaningful error handling, and when that behavior is visible through the existing runtime lifecycle and event model.
