# UI Integration First-Slice Checklist

## Purpose

Turn the revised integration plan into a concrete, executable first slice for connecting Hermes WebUI to Hermes Core.

## Working assumption

The first slice should be narrow and should only prove one end-to-end path:

- WebUI starts a workflow run in Hermes Core,
- the UI can poll for status,
- the UI can retrieve artifacts,
- the UI can handle one approval or resume action,
- the existing UI path remains intact when the integration is disabled.

## Files to touch

### Hermes Core

1. [engine/runtime_service.py](engine/runtime_service.py)
   - Extend it with a small run-state lifecycle API for UI-style operations.
   - Add methods for start, status, events, artifacts, and approval/resume handling.

2. [engine/api.py](engine/api.py)
   - Expose the new lifecycle methods through a thin API wrapper.
   - Keep the wrapper narrow and stable for the UI boundary.

3. [engine/http_adapter.py](engine/http_adapter.py)
   - Use this file as the initial bridge adapter surface for UI requests.
   - Add request dispatch for the new lifecycle actions.

4. [engine/models.py](engine/models.py)
   - Add any minimal data structures needed for UI-facing run state and event payloads if they do not already exist.

5. [engine/test_runtime_service.py](engine/test_runtime_service.py)
   - Add tests for start/status/artifact/approval behavior.

6. [engine/test_api.py](engine/test_api.py)
   - Add tests for the API wrapper boundary.

### Hermes WebUI

The WebUI changes belong in the sibling Hermes WebUI repository, not in this repository.

7. WebUI runtime adapter seam
   - Implement a Hermes Core adapter that satisfies the existing RuntimeAdapter protocol.
   - Keep the adapter thin and translate between the WebUI contract and the Hermes Core bridge.

8. WebUI route entrypoint
   - Ensure the existing runtime adapter path can be selected for the new Hermes Core mode.
   - Keep the old path unchanged when the feature is disabled.

9. WebUI runner client boundary (optional)
   - Reuse the existing runner-client pattern if the Hermes Core bridge is exposed over HTTP.
   - If not, this can be skipped and replaced by a simpler local adapter client.

10. WebUI adapter mode plumbing
   - Add any adapter mode plumbing needed for a new Hermes Core mode.
   - This should be wired through the existing adapter selection logic rather than added as a separate path.

## Implementation order

### Step 1 — Create the contract

Define the minimal request/response shape for:

- start run,
- observe run,
- get run,
- get artifacts,
- approve/resume run.

This should be documented in a small schema or docstring in the bridge layer.

### Step 2 — Add the bridge in Hermes Core

Implement the smallest possible lifecycle service that can:

- create a run record,
- store workflow input and status,
- expose progress events,
- return artifacts,
- handle approval/resume.

The first implementation can be backed by the existing runtime kernel and persisted storage rather than a separate new engine.

### Step 3 — Expose the bridge through Hermes Core API

Add methods on the API wrapper so WebUI can call into the bridge without importing Hermes Core runtime internals directly.

### Step 4 — Implement the WebUI adapter

Build the first concrete adapter in the WebUI repository that uses the Hermes Core bridge.

This should be wired through the existing runtime adapter selection logic rather than added as a parallel execution path.

The adapter should implement only:

- start_run,
- observe_run,
- get_run,
- get artifacts support via the run status payload,
- approval/resume control methods.

### Step 5 — Add feature-flag wiring

Add a dedicated environment variable or config flag so the integration is off by default.

Suggested approach:

- keep the existing path as the default,
- enable the Hermes Core adapter only when the flag is set.

### Step 6 — Add tests

Add tests in the following order:

1. core bridge start/status/artifact behavior,
2. core API wrapper behavior,
3. WebUI adapter translation behavior,
4. one end-to-end path with the feature flag enabled.

## Acceptance criteria

The first slice should be validated with a deterministic workflow such as hello_world or approval_example, not with a real external model provider.

The first slice is complete when all of the following are true:

- WebUI can start a workflow run through the Hermes Core bridge.
- WebUI can poll for status through the adapter.
- WebUI can retrieve artifacts for the run.
- One approval or resume action works through the same path.
- The original path still works when the feature flag is off.
- The new path is covered by regression tests.

## Risks to watch closely

1. The biggest risk is trying to support too much too early.
   - Do not add full agent-session integration in the first slice.
   - Do not attempt arbitrary workflow orchestration yet.

2. The second risk is coupling WebUI to Hermes Core internals.
   - Keep the adapter and bridge as the only translation layer.

3. The third risk is building a bridge that does not preserve state across polls.
   - The run registry and event stream must be persistent enough for UI polling.

## Recommended implementation milestone

Complete the first slice in this order:

1. contract,
2. bridge,
3. adapter,
4. feature flag,
5. tests.
