import json
import unittest

from engine.models import WorkerRequest
from workers.local_worker import LocalWorker
from workers.model_adapter import (
    ChatResult,
    ModelAdapter,
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


class TestOllamaToolLoop(unittest.TestCase):
    """OllamaModelAdapter.generate_with_tools against a mocked /v1/chat/completions."""

    TOOL_SCHEMA = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"action": {"type": "string"}, "path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ]

    def _adapter(self):
        config = ModelAdapterConfig(
            provider="ollama",
            model_name="qwen3.8:27b",
            endpoint="http://localhost:11434",
        )
        return OllamaModelAdapter(config)

    class _FakeResponse:
        def __init__(self, body: str) -> None:
            self._body = body.encode("utf-8")
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    def _monkeypatch_urlopen(self, responses):
        import workers.model_adapter as ma

        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append({"url": req.full_url, "data": req.data, "headers": dict(req.header_items())})
            body = responses[min(len(calls) - 1, len(responses) - 1)]
            return self._FakeResponse(body)

        original = ma.request.urlopen
        ma.request.urlopen = fake_urlopen
        self.addCleanup(lambda: setattr(ma.request, "urlopen", original))
        return calls

    def test_tool_loop_executes_call_and_converges(self):
        import json as _json
        adapter = self._adapter()
        turn1 = {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"id": "call_abc", "index": 0, "type": "function",
                             "function": {"name": "read_file", "arguments": _json.dumps({"action": "read", "path": "/etc/hostname"})}}
                        ],
                    },
                }
            ]
        }
        turn2 = {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "hostname is probe-1234", "tool_calls": None},
                }
            ]
        }
        captured = self._monkeypatch_urlopen([_json.dumps(turn1), _json.dumps(turn2)])

        dispatched = []

        def executor(tool_id, action, params):
            dispatched.append((tool_id, action, params))
            return {"content": "probe-1234", "path": params.get("path")}

        result = adapter.generate_with_tools(
            [{"role": "user", "content": "read the hostname"}],
            self.TOOL_SCHEMA,
            tool_executor=executor,
            max_iterations=8,
        )

        self.assertEqual(result.text, "hostname is probe-1234")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.iterations, 2)
        self.assertEqual(len(result.tool_calls), 1)
        tool_id, action, params, out_json = result.tool_calls[0]
        self.assertEqual(tool_id, "read_file")
        self.assertEqual(action, "read")
        self.assertEqual(params, {"path": "/etc/hostname"})
        self.assertEqual(_json.loads(out_json)["content"], "probe-1234")
        self.assertEqual(dispatched, [("read_file", "read", {"path": "/etc/hostname"})])

    def test_request_shape_omits_tool_choice_and_posts_v1_url(self):
        adapter = self._adapter()
        body = {"choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]}
        captured = self._monkeypatch_urlopen([json.dumps(body)])

        adapter.generate_with_tools(
            [{"role": "user", "content": "hi"}],
            self.TOOL_SCHEMA,
            tool_executor=lambda *a: "ok",
            max_iterations=2,
        )

        self.assertEqual(len(captured), 1)
        call0 = captured[0]
        self.assertEqual(call0["url"], "http://localhost:11434/v1/chat/completions")
        payload = json.loads(call0["data"].decode("utf-8"))
        self.assertNotIn("tool_choice", payload)
        self.assertEqual(payload["model"], "qwen3.8:27b")
        self.assertTrue(payload["tools"])
        self.assertFalse(payload["stream"])

    def test_max_iterations_stops_infinite_tool_loop(self):
        adapter = self._adapter()
        body = {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"id": "call_x", "index": 0, "type": "function",
                             "function": {"name": "read_file", "arguments": "{}"}}
                        ],
                    },
                }
            ]
        }
        captured = self._monkeypatch_urlopen([json.dumps(body)])
        result = adapter.generate_with_tools(
            [{"role": "user", "content": "loop"}],
            self.TOOL_SCHEMA,
            tool_executor=lambda *a: "x",
            max_iterations=3,
        )
        self.assertEqual(result.finish_reason, "max_iterations_reached")
        self.assertEqual(result.iterations, 3)
        self.assertEqual(len(result.tool_calls), 3)


class TestLocalWorker(unittest.TestCase):
    """LocalWorker: native tool-calling path and legacy single-shot path."""

    def _worker_request(self, **overrides):
        base = {
            "execution_id": "ex-1",
            "workflow_id": "wf-1",
            "role": "worker:local",
            "objective": {"description": "Read the file /etc/hostname and report it."},
            "context": {"expected_outputs": ["report"]},
            "constraints": {"allowed_tools": ["read_fs"]},
        }
        base.update(overrides)
        return WorkerRequest(**base)

    def test_native_path_drives_tool_loop(self):
        class FakeAdapter(ModelAdapter):
            def generate(self, prompt, *, temperature=0.2, max_tokens=1024):
                raise AssertionError("native path must not call generate()")

            def capabilities(self):
                return {"provider": "fake", "capabilities": {"tool_use": True}}

            def generate_with_tools(self, messages, tools, *, tool_executor=None, max_iterations=8,
                                    temperature=None, max_tokens=None):
                assert messages[0]["role"] == "user"
                assert tools and tools[0]["function"]["name"] == "read_fs"
                assert tool_executor is not None
                out = tool_executor("read_fs", "read", {"path": "/etc/hostname"})
                return ChatResult(
                    text="hostname is probe-1234",
                    tool_calls=[["read_fs", "read", {"path": "/etc/hostname"}, out]],
                    iterations=2,
                    finish_reason="stop",
                )

        calls = []

        def executor(tool_id, action, params):
            calls.append((tool_id, action, params))
            return "probe-1234"

        worker = LocalWorker(FakeAdapter(), tools=[{"type": "function",
            "function": {"name": "read_fs", "description": "d", "parameters": {"type": "object"}}}],
            tool_executor=executor)
        response = worker.execute(self._worker_request())

        self.assertEqual(response.status, "completed")
        self.assertEqual(len(response.artifacts_created), 1)
        payload = response.artifacts_created[0].content
        self.assertEqual(payload["model_output"], "hostname is probe-1234")
        self.assertEqual(len(payload["tool_calls"]), 1)
        self.assertEqual(payload["finish_reason"], "stop")
        self.assertEqual(len(response.recommendations), 1)
        self.assertEqual(response.recommendations[0].tool_id, "read_fs")
        self.assertEqual(response.recommendations[0].action, "read")
        self.assertEqual(calls, [("read_fs", "read", {"path": "/etc/hostname"})])

    def test_legacy_path_still_works_without_tools(self):
        adapter = StubModelAdapter(ModelAdapterConfig(provider="stub"))
        worker = LocalWorker(adapter)

        # No allowed tools: no recommendations at all.
        request = self._worker_request(constraints={})
        response = worker.execute(request)
        self.assertEqual(response.status, "completed")
        self.assertEqual(response.recommendations, [])
        self.assertIn("stub response", response.artifacts_created[0].content["model_output"])

    def test_legacy_path_emits_recommendation_per_allowed_tool(self):
        adapter = StubModelAdapter(ModelAdapterConfig(provider="stub"))
        worker = LocalWorker(adapter)
        request = self._worker_request(constraints={"allowed_tools": ["a", "b"]})
        response = worker.execute(request)
        self.assertEqual([r.tool_id for r in response.recommendations], ["a", "b"])
        self.assertTrue(all(r.action == "invoke" for r in response.recommendations))


if __name__ == "__main__":
    unittest.main()
