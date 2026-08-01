# Model routing and provider layer implementation plan

## Goal

Add a routing layer that can send work to the cheapest or most appropriate model available while keeping fallback behavior robust and cheap. This should make Hermes usable with local models first and remote providers second when needed.

## Why this matters

The current model abstraction in [workers/model_adapter.py](workers/model_adapter.py) is minimal and only supports stub and Ollama-style behavior. The system needs a more capable routing layer if it is going to be useful for everyday tasks without paying for expensive models on every request.

## Proposed architecture

### Core components

- Provider adapter interface: a shared interface for local and remote backends.
- Provider registry: known providers and their capabilities.
- Router policy: decides which provider should handle the current request.
- Health monitor: tracks latency, failures, rate limits, and provider availability.
- Usage tracker: tracks cost, token usage, and fallback frequency.

### External acceleration strategy

- Use LiteLLM as the initial provider abstraction layer rather than building custom provider plumbing from scratch.
- Keep the router policy simple at first: cheap/local first, stronger provider only when the task type or verification result indicates that it is needed.
- Avoid over-engineering provider-specific logic in the first version; treat providers as adapters behind a narrow interface.

## Implementation steps

### Phase 1: Define provider capabilities

- Define a capability model for each provider, including:
  - reasoning quality
  - coding strength
  - tool use support
  - latency
  - cost class
  - privacy constraints
- Use the existing capability model in [engine/capability.py](engine/capability.py) as the starting point.

### Phase 2: Add a provider adapter interface

- Introduce a generic provider interface that exposes:
  - availability checks
  - request execution
  - capability reporting
  - error handling
- Implement adapters for:
  - local Ollama or LM Studio
  - a cheap remote provider
  - a stronger fallback provider

### Phase 3: Add routing policy

- Route simple tasks to cheaper providers.
- Escalate to stronger providers when the task requires more reasoning, code generation, or tool use.
- Keep routing heuristics explicit and easy to tune.

### Phase 4: Add failover and circuit breaking

- If a provider fails, retry with the next candidate.
- Mark unhealthy providers temporarily unavailable.
- Keep the router from hammering failing backends.

### Phase 5: Add usage and budget tracking

- Track requests, failures, cost, and latency.
- Add a configurable budget policy so that cheap runs remain cheap.
- Emit a simple usage report at the end of each job.

## Deliverables

- Provider interface and provider registry
- Router policy module
- Health monitoring and failover logic
- Usage and cost reporting
- A simple configuration surface for provider preferences

## Acceptance criteria

- The system can route a simple task to a local or cheap provider by default.
- The router can escalate to a stronger provider when the task requires it.
- The system can recover from provider failure without crashing the whole run.

## Risks and mitigations

- Risk: routing heuristics are too simplistic and overuse expensive models.
  - Mitigation: start with a small explicit policy and measure actual usage.
- Risk: provider-specific integrations become too custom.
  - Mitigation: keep the interface generic and treat provider details as adapters.
