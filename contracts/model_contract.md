# Model Contract

## Purpose

Defines the adapter interface between Hermes Core and model providers.

## Interface

A model adapter must expose Hermes-specific capabilities and a minimal generation entry point.

Example contract:

```python
class ModelAdapter:
    def generate(self, prompt: str, *, temperature: float = 0.2, max_tokens: int = 1024) -> str:
        raise NotImplementedError

    def capabilities(self) -> dict:
        raise NotImplementedError
```

## Capability metadata

The adapter should describe its runtime capabilities to Hermes Core.

Example capability metadata:

```json
{
  "name": "qwen3-coder-30b",
  "provider": "ollama",
  "capabilities": {
    "code": true,
    "reasoning": true,
    "planning": true,
    "tool_use": false,
    "vision": false,
    "structured_output": true
  },
  "context_window": 262144,
  "max_tokens": 131072
}
```

## Request/response

- `generate(prompt, ...)` returns raw generated text.
- `capabilities()` returns supported model capabilities and feature flags.

The adapter should keep the interface minimal and Hermes-focused. Provider-specific features should not be exposed until there is concrete implementation experience.

## Implementations

Adapters should be implemented for each provider behind the same interface:

- `OllamaAdapter`
- `OpenAIAdapter`
- `AnthropicAdapter`
- `LocalModelAdapter`

## Worker usage

Workers should not depend on provider details.
Workers declare capability needs to Hermes, and Hermes resolves the appropriate model adapter.

Example capability request:

```python
capability_request = {
    "role": "developer",
    "required_capabilities": ["code", "reasoning"],
    "required_capabilities_map": {
        "tool_use": True,
        "vision": False
    }
}
model_adapter = hermes.resolve_model(capability_request)
response = model_adapter.generate(prompt)
```

This keeps workers decoupled from the underlying model provider and avoids hidden model selection logic inside the worker.

## Separation of concerns

- Hermes Core resolves model adapters based on capability requirements.
- Worker implementations focus on capability behavior, not provider plumbing.
