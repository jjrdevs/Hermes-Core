# ADR-003: Storage Choice

## Status

Proposed

## Context

Hermes must persist workflow state, artifact lineage, and event history. The first implementation should be simple, reliable, and easy to inspect.

## Decision

- Use SQLite for the first persistence layer.
- Model persistence as:
  - an event log for all state-changing operations
  - a metadata-backed artifact store for immutable artifacts
  - a small state snapshot for workflow recovery

## Rationale

- SQLite is widely available and requires no external database service.
- It is sufficient for local experimentation and initial end-to-end proof of concept.
- The event log design isolates state derivation from storage.

## Consequences

Positive:

- Fast setup for the first implementation.
- Easy reproducibility and debugging.
- Clear migration path to Postgres or another service later.

Negative:

- Not horizontally scalable in the initial version.
- Requires migration design for future distributed deployments.

## Future

- Later storage target: PostgreSQL or managed cloud database.
- Keep persistence abstractions so adapters can swap backends without changing orchestration.
