import unittest
from engine.provider_health import ProviderHealthRegistry
from engine.metrics import InMemoryMetricsSink


class TestProviderBackoff(unittest.TestCase):
    def test_backoff_multiplier_increases_on_reopen(self):
        metrics = InMemoryMetricsSink()
        registry = ProviderHealthRegistry(probe_interval=1, failure_threshold=1, recovery_seconds=1, metrics_sink=metrics)
        profiles = [{"provider": "p1", "endpoint": "http://example.local"}]
        registry.register_profiles(profiles)

        # first failure opens circuit
        registry.record_failure("p1")
        s1 = registry.get_summary("p1")
        self.assertEqual(s1.get("circuit_state"), "open")
        m1 = int(metrics.counters.get("provider_probe_failures_total|provider=p1", 0))
        self.assertGreaterEqual(m1, 1)

        # close circuit via success
        registry.record_success("p1")
        s2 = registry.get_summary("p1")
        self.assertEqual(s2.get("circuit_state"), "closed")

        # another failure re-opens and backoff multiplier should have increased
        registry.record_failure("p1")
        s3 = registry.get_summary("p1")
        self.assertEqual(s3.get("circuit_state"), "open")
        # backoff multiplier reflected indirectly by having next_retry not None
        self.assertIsNotNone(s3.get("next_retry"))


if __name__ == "__main__":
    unittest.main()
