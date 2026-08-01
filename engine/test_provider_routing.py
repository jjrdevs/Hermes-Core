import tempfile
import unittest
from pathlib import Path

from engine.runtime_service import RuntimeService


class TestProviderRouting(unittest.TestCase):
    def test_start_run_records_provider_routing(self):
        with tempfile.TemporaryDirectory(prefix="hermes-provider-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                workflow_path = str(Path(__file__).resolve().parent.parent / "examples" / "hello_world.json")
                result = service.start_run(workflow_path, provider="stub")
                run_id = result.get("run_id") or result.get("execution_id")
                run = service.get_run(run_id)
                self.assertIn("provider_routing", run)
                routing = run.get("provider_routing")
                self.assertIsInstance(routing, dict)
                self.assertIn("selected_provider", routing)
                self.assertIn("provider_health", routing)
            finally:
                service.shutdown()


if __name__ == "__main__":
    unittest.main()
