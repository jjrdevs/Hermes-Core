# Phase 3 Backlog — Provider Routing & Telemetry

This file tracks remaining Phase 3 items for future implementation and operationalization.

Remaining items:

- Continuous provider health monitoring
  - Periodic probes (latency and availability checks) per provider
  - Rolling success/failure counters and moving averages
  - Health state transitions with decay/aging and automated marking of degraded/unavailable

- Retry and fallback orchestration
  - Backoff strategies per provider class (immediate, deferred, skipped)
  - Dynamic fallback rules based on recent failures and cost thresholds
  - Circuit-breaker behavior for repeated failures

- Telemetry & metrics export
  - Prometheus metrics or a pluggable metrics sink
  - Histograms for latency, token consumption, request sizes
  - Counters for routing decisions, fallback uses, and provider errors
  - Per-request token/cost reporting persisted in run summaries

- Route-level cost accounting
  - Track cost_per_token and compute estimated cost per request
  - Aggregate cost per run and per tenant (if multi-tenant)

- Alerting and observable dashboards
  - Threshold-based alerts for provider degradation (high error-rate or slow latency)
  - Summary dashboards for routing, cost, and provider health over time

- Operational hooks
  - Admin endpoints to mark providers healthy/unhealthy or adjust weights
  - Exported routing logs for audit and offline analysis

Notes:
- `workers/model_adapter.py` already emits a routing health summary for each route decision (timeout classification, cost_per_token, health_status, retry_classification). The backlog items above focus on making routing state continuous, aggregated, and exported to monitoring.

Considerations for implementation:
- Keep routing and health state pluggable behind a `ProviderHealthRegistry` interface so different deployments can plug in their own monitors.
- Avoid automatic provider removal without operator confirmation for critical providers until circuits and fallbacks are well-tested.
