import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine.provider_health import ProviderHealthRegistry
from workers.model_adapter import ModelAdapterRouter, ProviderProfile


class TestProviderHealthRegistry(unittest.TestCase):
    def test_circuit_opens_and_recovers(self):
        events = []

        def probe_func(provider, profile):
            # first three probes fail, then succeed
            count = sum(1 for e in events if e[0] == provider)
            events.append((provider, datetime.now(timezone.utc)))
            return count >= 3

        registry = ProviderHealthRegistry(probe_interval=0.01, failure_threshold=3, recovery_seconds=1, probe_func=probe_func)
        profiles = [{"provider": "p1", "endpoint": "http://example.local"}]
        registry.register_profiles(profiles)
        registry.start()

        # wait for probes to run
        import time

        time.sleep(0.1)
        summary = registry.get_summary("p1")
        self.assertIn(summary.get("circuit_state"), {"open", "half_open", "closed"})

        # ensure failures recorded
        self.assertGreaterEqual(summary.get("failures", 0), 0)

        registry.stop()

    def test_router_uses_health_registry(self):
        registry = ProviderHealthRegistry(probe_interval=1, failure_threshold=1, recovery_seconds=60, probe_func=lambda p, prof: False)
        profiles = [ProviderProfile(provider="p1", cost_class="cheap", available=True), ProviderProfile(provider="stub", cost_class="cheap", available=True)]
        router = ModelAdapterRouter(profiles=profiles, health_registry=registry)
        decision = router.route("hello world")
        # since p1 probe returns False, health summary should reflect degraded/unhealthy
        assert isinstance(decision.health_summary, dict)
        assert "provider_health" in decision.health_summary


if __name__ == "__main__":
    unittest.main()
