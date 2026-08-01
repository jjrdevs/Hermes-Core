import unittest

from workers.model_adapter import (
    ModelAdapterConfig,
    ModelAdapterFactory,
    ModelAdapterRouter,
    OllamaModelAdapter,
    ProviderProfile,
    RoutingDecision,
    StubModelAdapter,
)


class TestModelAdapters(unittest.TestCase):
    def test_factory_builds_stub_by_default(self):
        adapter = ModelAdapterFactory.create(ModelAdapterConfig(provider="stub"))
        self.assertIsInstance(adapter, StubModelAdapter)

    def test_factory_builds_ollama_adapter_from_config(self):
        config = ModelAdapterConfig(
            provider="ollama",
            model_name="qwen3-coder-30b",
            endpoint="http://localhost:11434",
        )
        adapter = ModelAdapterFactory.create(config)
        self.assertIsInstance(adapter, OllamaModelAdapter)
        self.assertEqual(adapter.capabilities()["provider"], "ollama")
        self.assertEqual(adapter.capabilities()["name"], "qwen3-coder-30b")

    def test_router_prefers_stub_for_simple_tasks(self):
        router = ModelAdapterRouter(
            profiles=[
                ProviderProfile(provider="stub", cost_class="cheap", available=True),
                ProviderProfile(provider="ollama", cost_class="local", available=True),
            ]
        )

        decision = router.route("Summarize the repository", task_complexity="simple")

        self.assertIsInstance(decision, RoutingDecision)
        self.assertEqual(decision.provider, "stub")
        self.assertFalse(decision.fallback_used)
        self.assertIsInstance(decision.adapter, StubModelAdapter)

    def test_router_falls_back_when_preferred_provider_is_unavailable(self):
        router = ModelAdapterRouter(
            profiles=[
                ProviderProfile(provider="ollama", cost_class="local", available=False),
                ProviderProfile(provider="stub", cost_class="cheap", available=True),
            ],
            preferred_provider="ollama",
        )

        decision = router.route("Implement a small helper function", task_complexity="moderate")

        self.assertEqual(decision.provider, "stub")
        self.assertTrue(decision.fallback_used)
        self.assertIsInstance(decision.adapter, StubModelAdapter)

    def test_router_falls_back_when_preferred_provider_is_unhealthy(self):
        router = ModelAdapterRouter(
            profiles=[
                ProviderProfile(provider="ollama", cost_class="local", available=True, healthy=False),
                ProviderProfile(provider="stub", cost_class="cheap", available=True, healthy=True),
            ],
            preferred_provider="ollama",
        )

        decision = router.route("Implement a small helper function", task_complexity="moderate")

        self.assertEqual(decision.provider, "stub")
        self.assertTrue(decision.fallback_used)
        self.assertIn("unhealthy", decision.reason.lower())
        self.assertIsInstance(decision.adapter, StubModelAdapter)

    def test_router_reports_health_summary_for_fallback_decisions(self):
        router = ModelAdapterRouter(
            profiles=[
                ProviderProfile(provider="ollama", cost_class="local", available=True, healthy=False, endpoint="http://localhost:11434"),
                ProviderProfile(provider="stub", cost_class="cheap", available=True, healthy=True),
            ],
            preferred_provider="ollama",
        )

        decision = router.route("Implement a small helper function", task_complexity="moderate")

        self.assertTrue(decision.fallback_used)
        self.assertEqual(decision.health_summary["preferred_provider"], "ollama")
        self.assertEqual(decision.health_summary["selected_provider"], "stub")
        self.assertTrue(decision.health_summary["provider_health"]["stub"]["healthy"])
        self.assertFalse(decision.health_summary["provider_health"]["ollama"]["healthy"])
        self.assertIn("ollama", decision.health_summary["provider_health"])

    def test_router_reports_timeout_and_cost_metadata(self):
        router = ModelAdapterRouter(
            profiles=[
                ProviderProfile(
                    provider="ollama",
                    cost_class="local",
                    available=True,
                    healthy=False,
                    timeout_seconds=10.0,
                    endpoint="http://localhost:11434",
                    options={"cost_per_token": 0.000002},
                ),
                ProviderProfile(
                    provider="stub",
                    cost_class="cheap",
                    available=True,
                    healthy=True,
                    timeout_seconds=2.0,
                    options={"cost_per_token": 0.0},
                ),
            ],
            preferred_provider="ollama",
        )

        decision = router.route("Implement a small helper function", task_complexity="moderate")

        self.assertTrue(decision.fallback_used)
        self.assertEqual(decision.health_summary["provider_health"]["ollama"]["timeout_classification"], "moderate")
        self.assertEqual(decision.health_summary["provider_health"]["stub"]["timeout_classification"], "fast")
        self.assertEqual(decision.health_summary["provider_health"]["ollama"]["cost_per_token"], 0.000002)
        self.assertEqual(decision.health_summary["provider_health"]["stub"]["cost_per_token"], 0.0)
        self.assertEqual(decision.health_summary["provider_health"]["ollama"]["health_status"], "degraded")
        self.assertEqual(decision.health_summary["provider_health"]["stub"]["health_status"], "healthy")
        self.assertEqual(decision.health_summary["provider_health"]["ollama"]["retry_classification"], "deferred")
        self.assertEqual(decision.health_summary["provider_health"]["stub"]["retry_classification"], "immediate")


if __name__ == "__main__":
    unittest.main()
