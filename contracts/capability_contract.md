# Capability Contract

## Purpose

Defines the capability vocabulary Hermes Core uses to resolve worker and model selection.

## Capability model

A capability describes what a worker or model can do, independent of implementation.

Example capability schema:

```json
{
  "capability": "software_development",
  "requirements": [
    "code_generation",
    "reasoning",
    "large_context"
  ],
  "preferred_models": [
    "qwen3-coder-30b",
    "claude-opus"
  ]
}
```

## Common capabilities

- `architect`
- `developer`
- `researcher`
- `reviewer`
- `qa`
- `security_auditor`
- `planner`
- `operator`

## Capability metadata

Hermes should maintain a registry of capability definitions.

Example metadata:

```json
{
  "capability": "developer",
  "required_features": ["code", "repair", "test_generation"],
  "preferred_context_window": 65536,
  "requires_tool_support": true
}
```

## Usage

- Workers are instantiated or selected based on their declared capability.
- Models are chosen based on the capability request from Hermes.
- Policies may act on capabilities to allow or deny execution.
