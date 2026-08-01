import tempfile
import unittest
from pathlib import Path

from engine.http_adapter import HermesHttpAdapter


class TestHermesHttpAdapter(unittest.TestCase):
    def test_adapter_routes_run_and_query_requests(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = HermesHttpAdapter(data_dir=temp_dir)
            try:
                result = adapter.handle({"action": "run_workflow", "workflow_path": str(workflow_path)})
                self.assertEqual(result["workflow_name"], "hello_world")
                self.assertEqual(result["status"], "COMPLETED")

                summary = adapter.handle({"action": "get_execution_summary", "execution_id": result["execution_id"]})
                self.assertEqual(summary["status"], "COMPLETED")

                artifacts = adapter.handle({"action": "get_artifacts", "execution_id": result["execution_id"]})
                self.assertTrue(artifacts)
            finally:
                adapter.shutdown()

    def test_adapter_rejects_ui_runtime_actions_when_disabled(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = HermesHttpAdapter(data_dir=temp_dir)
            try:
                result = adapter.handle({"action": "start_run", "workflow_path": str(workflow_path)})
                self.assertFalse(result.get("accepted", True))
                self.assertEqual(result.get("status"), "disabled")
            finally:
                adapter.shutdown()

    def test_feature_flag_enabled_adapter_supports_lifecycle_actions(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "approval_example.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = HermesHttpAdapter(data_dir=temp_dir, enabled=True)
            try:
                started = adapter.handle({"action": "start_run", "workflow_path": str(workflow_path), "context": {"execution_mode": "manual"}})
                self.assertEqual(started["status"], "WAITING_APPROVAL")
                self.assertIn("run_id", started)

                observed = adapter.handle({"action": "observe_run", "run_id": started["run_id"]})
                self.assertEqual(observed["run_id"], started["run_id"])

                artifacts = adapter.handle({"action": "get_run_artifacts", "run_id": started["run_id"]})
                self.assertTrue(artifacts)
            finally:
                adapter.shutdown()

    def test_adapter_accepts_alias_actions_for_runtime_contract(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = HermesHttpAdapter(data_dir=temp_dir, enabled=True)
            try:
                started = adapter.handle({"action": "start", "workflow_path": str(workflow_path)})
                self.assertIn("run_id", started)

                status = adapter.handle({"action": "status", "run_id": started["run_id"]})
                self.assertEqual(status["run_id"], started["run_id"])

                observed = adapter.handle({"action": "observe", "run_id": started["run_id"]})
                self.assertEqual(observed["run_id"], started["run_id"])
            finally:
                adapter.shutdown()

    def test_adapter_supports_generic_control_envelope(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = HermesHttpAdapter(data_dir=temp_dir, enabled=True)
            try:
                started = adapter.handle({"action": "start_run", "workflow_path": str(workflow_path)})

                response = adapter.handle(
                    {
                        "action": "control",
                        "run_id": started["run_id"],
                        "control_action": "queue_message",
                        "payload": {"text": "still working"},
                        "actor": "agent",
                        "correlation_id": "corr-99",
                    }
                )
                self.assertTrue(response["accepted"])
            finally:
                adapter.shutdown()

    def test_adapter_surfaces_run_summary_for_observation_requests(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "approval_example.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = HermesHttpAdapter(data_dir=temp_dir, enabled=True)
            try:
                started = adapter.handle({"action": "start_run", "workflow_path": str(workflow_path), "context": {"execution_mode": "manual"}})
                observed = adapter.handle({"action": "observe_run", "run_id": started["run_id"]})

                self.assertIn("summary", observed)
                self.assertEqual(observed["summary"]["status"], "WAITING_APPROVAL")
                self.assertEqual(observed["summary"]["pending_approval_role"], "human_operator")
            finally:
                adapter.shutdown()

    def test_adapter_exposes_checkpoint_inspection_actions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            adapter = HermesHttpAdapter(data_dir=temp_dir, enabled=True)
            try:
                adapter.api.service.run_task("inspect the repository", workspace_path=temp_dir)
                checkpoints = adapter.handle({"action": "list_checkpoints"})
                self.assertTrue(checkpoints)

                first = checkpoints[0]
                loaded = adapter.handle({"action": "get_checkpoint", "checkpoint_id": first["checkpoint_id"]})
                self.assertEqual(loaded["checkpoint_id"], first["checkpoint_id"])
            finally:
                adapter.shutdown()


if __name__ == "__main__":
    unittest.main()
