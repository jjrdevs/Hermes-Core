import unittest

from workers.model_adapter import (
    ModelAdapterConfig,
    ModelAdapterFactory,
    OllamaModelAdapter,
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


if __name__ == "__main__":
    unittest.main()
