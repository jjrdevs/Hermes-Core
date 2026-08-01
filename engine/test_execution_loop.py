import unittest

from engine.runtime_service import RuntimeService
from workers.model_adapter import StubModelAdapter


class TestExecutionLoop(unittest.TestCase):
    def test_runtime_service_builds_review_plan(self):
        service = RuntimeService(data_dir="/tmp/hermes-test-runtime")
        result = service.run_task(
            "Inspect the repository and write a short plan",
            workspace_path="/tmp",
            context={"max_iterations": 2, "dry_run": True},
        )

        self.assertEqual(result["status"], "IN_PROGRESS")
        summary = result["summary"]
        self.assertIn("task_envelope", summary)
        self.assertIn("progress_artifacts", summary)
        self.assertIn("review_plan", summary)
        self.assertEqual(summary["review_plan"]["status"], "planned")

    def test_runtime_service_advances_execution_loop_phase_on_resume(self):
        service = RuntimeService(data_dir="/tmp/hermes-test-runtime")
        first = service.run_task(
            "Inspect the repository and write a short plan",
            workspace_path="/tmp",
            context={"max_iterations": 2, "dry_run": True},
        )

        self.assertEqual(first["status"], "IN_PROGRESS")
        self.assertEqual(first["summary"]["execution_loop"]["phase"], "plan")
        self.assertEqual(first["summary"]["execution_loop"]["iteration"], 1)

        resumed = service.run_task(
            "Inspect the repository and write a short plan",
            workspace_path="/tmp",
            context={"resume_from": first["checkpoint_id"], "max_iterations": 2, "dry_run": True},
        )

        self.assertEqual(resumed["status"], "COMPLETED")
        self.assertEqual(resumed["summary"]["execution_loop"]["phase"], "verify")
        self.assertEqual(resumed["summary"]["execution_loop"]["iteration"], 2)

    def test_runtime_service_emits_structured_execution_phase_state(self):
        service = RuntimeService(data_dir="/tmp/hermes-test-runtime")
        result = service.run_task(
            "Inspect the repository and write a short plan",
            workspace_path="/tmp",
            context={"max_iterations": 2, "dry_run": True},
        )

        self.assertEqual(result["status"], "IN_PROGRESS")
        self.assertIn("execution_loop", result["summary"])
        self.assertEqual(result["summary"]["execution_loop"]["phase"], "plan")
        self.assertEqual(result["summary"]["execution_loop"]["status"], "in_progress")

    def test_runtime_service_exposes_a_clear_stop_reason(self):
        service = RuntimeService(data_dir="/tmp/hermes-test-runtime")
        result = service.run_task(
            "Inspect the repository and write a short plan",
            workspace_path="/tmp",
            context={"max_iterations": 1, "dry_run": True},
        )

        self.assertIn("stop_reason", result["summary"]["execution_loop"])
        self.assertTrue(result["summary"]["execution_loop"]["stop_reason"])

    def test_runtime_service_sets_stop_reason_from_verification_failure(self):
        service = RuntimeService(data_dir="/tmp/hermes-test-runtime")
        result = service.run_task(
            "Run verification and report the outcome",
            workspace_path="/tmp",
            context={"max_iterations": 1, "dry_run": False},
        )

        self.assertEqual(result["summary"]["execution_loop"]["verification_status"], "not_run")
        self.assertIn("verification", result["summary"]["execution_loop"]["stop_reason"])


if __name__ == "__main__":
    unittest.main()
