# ADR-002: Hermes Runtime Architecture

## Status

Proposed

## Context

Hermes is intended to be a reusable orchestration core rather than a single point product UI. The initial implementation should maximize experimentation speed, integration with AI tooling, and developer-friendly deployment.

## Decision

- Use Python for the first implementation.
- Treat Hermes Core as an operational runtime with a programmable core API.
- Keep model adapters and worker implementations separate from orchestration logic.

## Rationale

- Python has the strongest AI ecosystem and easiest integration with model adapters.
- Python async makes orchestration and worker coordination simpler.
- Python enables fast iteration while still supporting later service or CLI deployment.
- An operational runtime keeps execution control inside Hermes and lets external interfaces plug in.

## Consequences

Positive:

- Easier integration with Ollama, OpenAI, Anthropic, and local inference.
- Clear separation of concerns between orchestration and execution.
- Better portability for future service or platform variants.
- Hermes retains execution authority; humans define goals and constraints.

Negative:

- Requires careful design to avoid over-generalization.
- Must define stable public core interfaces early.

## Architecture

Core layers:

- `engine/` — workflow execution, state, scheduling, event handling
- `workers/` — worker capability contracts and worker implementations
- `adapters/` — model provider adapters and runtime connectors
- `storage/` — artifact store, event log, persistence abstractions

## Runtime semantics

Hermes is not merely a developer framework. Hermes is an operations runtime capable of autonomous execution within declared goals, policies, and constraints.

Hermes autonomously executes authorized workflows. It does not autonomously redefine objectives, permissions, or system boundaries.

- The core owns execution flow.
- Intent, goals, constraints, and policies are declared by humans or higher-level systems.
- Hermes evaluates authorization and policy before execution.
- Model and worker selection are resolved by Hermes based on capability requirements.
- Interfaces such as CLI or API are adapters on top of the runtime.

## Notes

- The first vertical slice may still include a minimal CLI for manual execution.
- Do not treat Hermes as a model provider; models are adapters behind contract interfaces.
