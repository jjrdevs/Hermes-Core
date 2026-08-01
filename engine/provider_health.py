from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from .metrics import MetricsSink
import random
from urllib import request, error


class ProviderHealthRegistry:
    """Maintain health probes, failure counters and a simple circuit breaker per provider.

    This registry runs a background probe loop (optional) and exposes methods
    for recording success/failure and querying provider health and circuit state.
    """

    def __init__(
        self,
        probe_interval: float = 10.0,
        failure_threshold: int = 3,
        recovery_seconds: int = 60,
        probe_func: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
        metrics_sink: Optional[MetricsSink] = None,
    ) -> None:
        self.probe_interval = float(probe_interval)
        self.failure_threshold = int(failure_threshold)
        self.recovery_seconds = int(recovery_seconds)
        self.probe_func = probe_func
        self.metrics = metrics_sink

        self._profiles: Dict[str, Dict[str, Any]] = {}
        self._stats: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def register_profiles(self, profiles: List[Dict[str, Any]]) -> None:
        with self._lock:
            for p in profiles:
                name = p.get("provider")
                if not name:
                    continue
                self._profiles[name] = dict(p)
                stats = self._stats.setdefault(name, {})
                stats.setdefault("failures", 0)
                stats.setdefault("successes", 0)
                stats.setdefault("circuit_state", "closed")
                stats.setdefault("opened_at", None)
                stats.setdefault("backoff_multiplier", 0)
                # allow per-provider tuning
                stats.setdefault("base_backoff_seconds", int(p.get("backoff_seconds",  self.recovery_seconds)))
                stats.setdefault("jitter_fraction", float(p.get("jitter", 0.1)))

    def _probe(self, provider: str, profile: Dict[str, Any]) -> bool:
        # customizable probe for tests
        if self.probe_func is not None:
            try:
                return bool(self.probe_func(provider, profile))
            except Exception:
                return False

        endpoint = profile.get("endpoint")
        if not endpoint:
            # local or stub providers considered healthy
            return True
        try:
            req = request.Request(endpoint, method="HEAD")
            with request.urlopen(req, timeout=5) as resp:
                return 200 <= resp.status < 400
        except Exception:
            return False

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                items = list(self._profiles.items())
            for provider, profile in items:
                try:
                    ok = self._probe(provider, profile)
                    if ok:
                        self.record_success(provider)
                    else:
                        self.record_failure(provider)
                except Exception:
                    self.record_failure(provider)
            self._stop.wait(self.probe_interval)

    def record_failure(self, provider: str) -> None:
        with self._lock:
            stats = self._stats.setdefault(provider, {})
            stats["failures"] = int(stats.get("failures", 0)) + 1
            stats.setdefault("successes", 0)
            # open circuit if threshold exceeded
            if stats.get("circuit_state") != "open" and stats["failures"] >= self.failure_threshold:
                stats["circuit_state"] = "open"
                stats["opened_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                # increase backoff multiplier for subsequent openings
                stats["backoff_multiplier"] = int(stats.get("backoff_multiplier", 0)) + 1
            # emit metrics
            try:
                if self.metrics is not None:
                    self.metrics.incr("provider_probe_failures_total", labels={"provider": provider})
                    state_val = 1 if stats.get("circuit_state") == "open" else 0
                    self.metrics.gauge_set("provider_circuit_state", state_val, labels={"provider": provider})
            except Exception:
                pass

    def record_success(self, provider: str) -> None:
        with self._lock:
            stats = self._stats.setdefault(provider, {})
            stats["successes"] = int(stats.get("successes", 0)) + 1
            stats.setdefault("failures", 0)
            # close circuit on success
            if stats.get("circuit_state") == "open":
                stats["circuit_state"] = "closed"
                stats["opened_at"] = None
                stats["failures"] = 0
            try:
                if self.metrics is not None:
                    self.metrics.incr("provider_probe_successes_total", labels={"provider": provider})
                    self.metrics.gauge_set("provider_circuit_state", 0, labels={"provider": provider})
            except Exception:
                pass

    def get_summary(self, provider: str) -> Dict[str, Any]:
        with self._lock:
            profile = dict(self._profiles.get(provider) or {})
            stats = dict(self._stats.get(provider) or {})
        # evaluate circuit expiry
        circuit_state = stats.get("circuit_state") or "closed"
        opened_at = stats.get("opened_at")
        next_retry = None
        if circuit_state == "open" and opened_at:
            try:
                opened_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00")).astimezone(timezone.utc)
                backoff_multiplier = int(stats.get("backoff_multiplier", 0))
                base_seconds = int(stats.get("base_backoff_seconds", self.recovery_seconds))
                effective_recovery = min(base_seconds * (2 ** max(0, backoff_multiplier - 1)), 3600)
                # apply jitter
                jitter_frac = float(stats.get("jitter_fraction", 0.1))
                if jitter_frac > 0:
                    jitter_amount = effective_recovery * random.uniform(-jitter_frac, jitter_frac)
                    effective_recovery = max(0, effective_recovery + jitter_amount)
                if datetime.now(timezone.utc) - opened_dt >= timedelta(seconds=effective_recovery):
                    # allow probe attempt
                    circuit_state = "half_open"
                else:
                    next_retry = (opened_dt + timedelta(seconds=effective_recovery)).isoformat().replace("+00:00", "Z")
            except Exception:
                pass

        healthy = circuit_state != "open"

        # emit metrics snapshot
        try:
            if self.metrics is not None:
                self.metrics.gauge_set("provider_probe_failures", float(stats.get("failures", 0)), labels={"provider": provider})
                self.metrics.gauge_set("provider_probe_successes", float(stats.get("successes", 0)), labels={"provider": provider})
                state_val = 1 if circuit_state == "open" else (2 if circuit_state == "half_open" else 0)
                self.metrics.gauge_set("provider_circuit_state", state_val, labels={"provider": provider})
        except Exception:
            pass

        return {
            "provider": provider,
            "profile": profile,
            "failures": stats.get("failures", 0),
            "successes": stats.get("successes", 0),
            "circuit_state": circuit_state,
            "next_retry": next_retry,
            "healthy": healthy,
        }
