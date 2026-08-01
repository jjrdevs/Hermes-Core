from __future__ import annotations

from typing import Any, Dict, Optional

try:
    from prometheus_client import Counter, Gauge, generate_latest, CollectorRegistry, CONTENT_TYPE_LATEST
    from prometheus_client import start_http_server
except Exception:  # pragma: no cover - optional dependency
    Counter = None  # type: ignore
    Gauge = None  # type: ignore
    generate_latest = None  # type: ignore
    CollectorRegistry = None  # type: ignore
    CONTENT_TYPE_LATEST = None  # type: ignore
    start_http_server = None  # type: ignore

from .metrics import MetricsSink
from .prometheus_server import acquire_prometheus_server, release_prometheus_server


class PrometheusMetricsSink(MetricsSink):
    def __init__(self, port: int = 8000, addr: str = "0.0.0.0", auto_start: bool = True) -> None:
        if Counter is None:
            raise RuntimeError("prometheus_client is required for PrometheusMetricsSink")
        self.registry = CollectorRegistry()
        self._counters: Dict[str, Counter] = {}
        self._gauges: Dict[str, Gauge] = {}
        self._port = port
        self._addr = addr
        self._auto_start = auto_start
        self._started = False

    def _counter(self, name: str, labels: Optional[Dict[str, str]] = None) -> Counter:
        key = name
        if key not in self._counters:
            self._counters[key] = Counter(name, name, list((labels or {}).keys()), registry=self.registry)
        return self._counters[key]

    def _gauge(self, name: str, labels: Optional[Dict[str, str]] = None) -> Gauge:
        key = name
        if key not in self._gauges:
            self._gauges[key] = Gauge(name, name, list((labels or {}).keys()), registry=self.registry)
        return self._gauges[key]

    def incr(self, name: str, amount: int = 1, labels: Optional[Dict[str, str]] = None) -> None:
        if self._auto_start and not self._started:
            acquire_prometheus_server(port=self._port, addr=self._addr)
            self._started = True
        c = self._counter(name, labels)
        if labels:
            c.labels(**labels).inc(amount)
        else:
            c.inc(amount)

    def gauge_set(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        if self._auto_start and not self._started:
            acquire_prometheus_server(port=self._port, addr=self._addr)
            self._started = True
        g = self._gauge(name, labels)
        if labels:
            g.labels(**labels).set(value)
        else:
            g.set(value)

    def close(self) -> None:
        """Release the server lease for this sink instance."""
        if self._auto_start and self._started:
            release_prometheus_server()
            self._started = False


def start_prometheus_http_server(port: int = 8000, addr: str = "0.0.0.0") -> None:
    if start_http_server is None:
        raise RuntimeError("prometheus_client is not available")
    start_http_server(port, addr)