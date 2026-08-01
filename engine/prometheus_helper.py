from __future__ import annotations

from typing import Optional

from .prometheus_sink import PrometheusMetricsSink


def enable_prometheus_for_registry(registry, port: int = 8000, addr: str = "0.0.0.0", auto_start: bool = True) -> PrometheusMetricsSink:
    """Create a Prometheus sink, attach it to the provided registry.

    By default the sink will *not* start the HTTP exposition until the first metric is emitted.
    Set `auto_start=False` to disable the lazy start behavior.

    Returns the sink instance so callers can also use it directly.
    """
    sink = PrometheusMetricsSink(port=port, addr=addr, auto_start=auto_start)
    # attach sink to registry so metrics emitted by registry will use Prometheus
    try:
        registry.metrics = sink
    except Exception:
        # best-effort; registry may expose different configuration API
        pass
    return sink
