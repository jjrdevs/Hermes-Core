# ADR-005: Capability-Based Execution

## Status

Proposed

## Context

Hermes must select execution capabilities and model adapters based on what needs to be done, not based on provider identity or concrete model names.

## Decision

Hermes will resolve workers and model adapters from capability requirements.

- Workers are selected by declared role/capability.
- Models are selected by capability and runtime characteristics.
- Execution decisions are based on capability metadata, not literal model names.

## Rationale

- This enables Hermes to route work dynamically to the best available resources.
- It decouples workflow definitions from specific providers.
- It supports future heterogenous environments with local and remote models.
- It preserves the operational control plane semantics.

## Capability model

A capability description includes:

- required features (`code`, `reasoning`, `planning`)
- preferred context window
- tool support requirements
- performance characteristics (`latency_class`, `cost_class`, `privacy_class`)

Example capability request:

```json
{
  "role": "developer",
  "required_capabilities": ["code", "reasoning"],
  "preferred_context_window": 65536,
  "tool_support": true,
  "priority": "high"
}
```

## Model selection

Hermes should choose available models via a capability registry that includes:

- provider metadata
- capability profile
- performance profile
- policy compatibility

Example model capability metadata:

```json
{
  "name": "qwen3-coder-30b",
  "provider": "ollama",
  "capabilities": ["code", "reasoning", "planning"],
  "context_window": 262144,
  "tool_support": false,
  "supports_functions": true,
  "performance": {
    "latency_class": "medium",
    "cost_class": "free",
    "privacy_class": "high"
  }
}
```

## Consequences

Positive:

- Hermes can route tasks to the most appropriate execution resource.
- Workflows remain stable even as model implementations change.
- Policy and cost controls become integral to execution.

Negative:

- The capability registry must be maintained accurately.
- More decision logic is required in the runtime.
