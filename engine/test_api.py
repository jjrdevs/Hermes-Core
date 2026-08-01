import tempfile
import unittest
from pathlib import Path

from engine.api import HermesApi


class TestHermesApi(unittest.TestCase):
    def test_api_can_run_and_query_workflow_state(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            api = HermesApi(data_dir=temp_dir)
            try:
                result = api.run_workflow(str(workflow_path))
                self.assertEqual(result["workflow_name"], "hello_world")
                self.assertEqual(result["status"], "COMPLETED")
                self.assertEqual(result["completed_steps"], ["say_hello"])

                summary = api.get_execution_summary(result["execution_id"])
                self.assertEqual(summary["status"], "COMPLETED")

                artifacts = api.get_artifacts(result["execution_id"])
                self.assertTrue(artifacts)
            finally:
                api.shutdown()

    def test_api_handle_accepts_shared_control_envelope(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            api = HermesApi(data_dir=temp_dir)
            try:
                started = api.handle({"action": "start_run", "workflow_path": str(workflow_path)})
                self.assertIn("run_id", started)

                response = api.handle(
                    {
                        "action": "control",
                        "run_id": started["run_id"],
                        "control_action": "queue_message",
                        "payload": {"text": "still working"},
                        "actor": "agent",
                        "correlation_id": "corr-123",
                    }
                )
                self.assertTrue(response["accepted"])
            finally:
                api.shutdown()

    def test_api_run_workflow_preserves_policy_context(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            api = HermesApi(data_dir=temp_dir)
            try:
                result = api.handle(
                    {
                        "action": "run_workflow",
                        "workflow_path": str(workflow_path),
                        "context": {"policy_context": {"approved": True, "policy_ids": ["api-approved"]}},
                    }
                )

                execution = api.service.kernel.workflow_executions[result["execution_id"]]
                self.assertEqual(execution.policy_context["policy_ids"], ["api-approved"])
            finally:
                api.shutdown()


if __name__ == "__main__":
    unittest.main()
