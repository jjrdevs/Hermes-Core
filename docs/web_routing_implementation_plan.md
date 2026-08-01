# Web routing implementation plan

## Goal

Wire Hermes so web search and web extract prefer a non-Firecrawl provider first when it is available, and only fall back to Firecrawl for recoverable failures or when the preferred path is unavailable.

## Current state

- The provider framework already exists in [agent/web_search_registry.py](agent/web_search_registry.py) and [tools/web_tools.py](tools/web_tools.py).
- Firecrawl is already wired and verified as working.
- The active runtime config in [config.yaml](config.yaml) currently pins the shared backend to Firecrawl, so the dispatcher is not yet using a true preference-based routing flow.

## Important audit findings

The main gap is not provider registration; it is dispatch behavior.

1. The existing selection helpers choose a backend based on availability, but they do not currently implement fallback after a provider fails at runtime.
2. Some providers are search-only and should not be used for extract.
3. A fallback should only happen for recoverable errors such as timeouts, connection failures, or provider-side errors, not for user input mistakes or blocked URLs.
4. Explicit user config overrides must continue to win.

## Proposed behavior

- Web search and web extract should use a configured preference order.
- If the preferred provider is unavailable, Hermes should try the next provider.
- If the preferred provider fails at runtime, Hermes should retry with the next provider only for recoverable errors.
- Firecrawl should remain the fallback target when the preferred path is unavailable or fails.

## Implementation steps

### 1. Define the routing policy clearly

- Keep the current config surface, but add an explicit priority order per capability:
  - search: preferred non-Firecrawl provider first, Firecrawl second
  - extract: preferred non-Firecrawl provider first, Firecrawl second
- Preserve the existing override behavior for:
  - web.search_backend
  - web.extract_backend
  - web.backend as the shared fallback

### 2. Update the backend selection helper

- Extend the current helper logic in [tools/web_tools.py](tools/web_tools.py) so it builds a candidate list instead of only returning a single backend.
- Keep the current availability checks, but make them part of a candidate ordering step.
- Do not silently bypass explicit config overrides.

### 3. Implement fallback at the dispatch layer

- Add fallback handling inside the search and extract dispatch path in [tools/web_tools.py](tools/web_tools.py).
- For each candidate provider:
  - attempt the call once
  - if it succeeds, return immediately
  - if it fails with a recoverable error, try the next candidate
  - if it is unsupported for the capability, skip it
  - if it is blocked by user input or URL policy, stop and return the error
- Keep the fallback bounded to one or two providers to avoid repeated retries and unnecessary cost.

### 4. Keep provider abstraction intact

- Reuse the existing registry and provider interface rather than introducing a one-off branch for Firecrawl.
- Make the fallback logic generic enough that future providers can be added without another dispatch rewrite.

### 5. Add regression tests

- Add tests for:
  - preferred provider selected first when available
  - fallback to Firecrawl when the preferred provider is unavailable
  - fallback to Firecrawl when the preferred provider errors at runtime
  - explicit config overrides still win
  - search-only providers are not selected for extract
- Use the existing test structure around [tests/tools/test_web_tools_config.py](tests/tools/test_web_tools_config.py) and related web provider tests as the starting point.

### 6. Verify end-to-end

- Validate the new routing path with real requests for:
  - web search
  - web extract
  - fallback behavior under a simulated provider failure
- Confirm that the selected provider and fallback reason are visible in logs or debug output.

## Expected failure points to guard against

- A provider can be available but still fail due to network issues or service-side errors.
- A search-only provider may be chosen for extract and fail in a confusing way.
- A provider can return a structured error that should trigger fallback, but a policy error should not.
- The implementation should not introduce infinite retries or silently hide the real reason for the failure.

## Acceptance criteria

- Web search and web extract use a configurable preference order.
- The dispatcher falls back to Firecrawl when the preferred path is unavailable or fails recoverably.
- Explicit backend overrides still take precedence.
- Search-only providers are excluded from extract routing.
- The behavior is covered by automated regression tests.

## Recommended rollout

1. Update the routing helper and dispatch path.
2. Add fallback handling for recoverable errors.
3. Add regression tests.
4. Verify with live web search and extract calls.
