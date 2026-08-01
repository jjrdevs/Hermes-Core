from __future__ import annotations

from typing import Any, Dict, Optional


class MetricsSink:
    def incr(self, name: str, amount: int = 1, labels: Optional[Dict[str, str]] = None) -> None:
        raise NotImplementedError

    def gauge_set(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        raise NotImplementedError


class InMemoryMetricsSink(MetricsSink):
    def __init__(self) -> None:
        self.counters: Dict[str, int] = {}
        self.gauges: Dict[str, float] = {}

    def _key(self, name: str, labels: Optional[Dict[str, str]] = None) -> str:
        if not labels:
            return name
        items = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}|{items}"

    def incr(self, name: str, amount: int = 1, labels: Optional[Dict[str, str]] = None) -> None:
        key = self._key(name, labels)
        self.counters[key] = self.counters.get(key, 0) + int(amount)

    def gauge_set(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        key = self._key(name, labels)
        self.gauges[key] = float(value)
