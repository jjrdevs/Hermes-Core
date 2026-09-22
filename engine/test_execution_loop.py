"""Execution-loop contract tests.

Each test runs inside its own isolated (hermetic) workspace so the results are
deterministic regardless of what happens to be in ``/tmp`` on the host. The
runtime discovers a verification command from the workspace (Makefile /
package.json / pyproject.toml); an empty workspace therefore yields
``verification_status == "not_run"``, which is what these assertions rely on.
"""

import os
import tempfile
import unittest

from engine.runtime_service import RuntimeService


class TestExecutionLoop(unittest.TestCase):
    def _make_service(self):
        """Return (service, workspace) in a fresh, empty, isolated directory."""
        root = tempfile.mkdtemp(prefix="hermes-execloop-")
        workspace = os.path.join(root, "ws")
        os.makedirs(workspace, exist_ok=True)
        data_dir = os.path.join(root, "data")
        os.makedirs(data_dir, exist_ok=True)
        service = RuntimeService(data_dir=data_dir)
        return service, workspace

    def test_runtime_service_builds_review_plan(self):
        service, workspace = self._make_service()
        result = service.run_task(
            "Inspect the repository and write a short plan",
            workspace_path=workspace,
            context={"max_iterations": 2, "dry_run": True},
        )

        self.assertEqual(result["status"], "IN_PROGRESS")
        summary = result["summary"]
        self.assertIn("task_envelope", summary)
        self.assertIn("progress_artifacts", summary)
        self.assertIn("review_plan", summary)
        self.assertEqual(summary["review_plan"]["status"], "planned")

    def test_runtime_service_advances_execution_loop_phase_on_resume(self):
        service, workspace = self._make_service()
        first = service.run_task(
            "Inspect the repository and write a short plan",
            workspace_path=workspace,
            context={"max_iterations": 2, "dry_run": True},
        )

        self.assertEqual(first["status"], "IN_PROGRESS")
        self.assertEqual(first["summary"]["execution_loop"]["phase"], "plan")
        self.assertEqual(first["summary"]["execution_loop"]["iteration"], 1)

        resumed = service.run_task(
            "Inspect the repository and write a short plan",
            workspace_path=workspace,
            context={"resume_from": first["checkpoint_id"], "max_iterations": 2, "dry_run": True},
        )

        self.assertEqual(resumed["status"], "COMPLETED")
        self.assertEqual(resumed["summary"]["execution_loop"]["phase"], "verify")
        self.assertEqual(resumed["summary"]["execution_loop"]["iteration"], 2)

    def test_runtime_service_emits_structured_execution_phase_state(self):
        service, workspace = self._make_service()
        result = service.run_task(
            "Inspect the repository and write a short plan",
            workspace_path=workspace,
            context={"max_iterations": 2, "dry_run": True},
        )

        self.assertEqual(result["status"], "IN_PROGRESS")
        self.assertIn("execution_loop", result["summary"])
        self.assertEqual(result["summary"]["execution_loop"]["phase"], "plan")
        self.assertEqual(result["summary"]["execution_loop"]["status"], "in_progress")

    def test_runtime_service_exposes_a_clear_stop_reason(self):
        service, workspace = self._make_service()
        result = service.run_task(
            "Inspect the repository and write a short plan",
            workspace_path=workspace,
            context={"max_iterations": 1, "dry_run": True},
        )

        self.assertIn("stop_reason", result["summary"]["execution_loop"])
        self.assertTrue(result["summary"]["execution_loop"]["stop_reason"])

    def test_runtime_service_sets_stop_reason_when_verification_not_run(self):
        service, workspace = self._make_service()
        result = service.run_task(
            "Run verification and report the outcome",
            workspace_path=workspace,
            context={"max_iterations": 1, "dry_run": False},
        )

        self.assertEqual(result["summary"]["execution_loop"]["verification_status"], "not_run")
        self.assertIn("verification", result["summary"]["execution_loop"]["stop_reason"])


if __name__ == "__main__":
    unittest.main()
