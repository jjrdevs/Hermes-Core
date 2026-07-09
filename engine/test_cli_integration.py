import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from engine.models import WorkflowDefinition

CLI = Path(__file__).resolve().parent.parent / "hermes_cli.py"


def run_cli(args, cwd=None, env=None):
    completed = subprocess.run(
        ["python3", str(CLI)] + args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


class TestHermesCLIIntegration(unittest.TestCase):
    def test_full_demo_workflow_approval_and_recovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            workflow_path = Path(__file__).resolve().parent.parent / "examples" / "design_review_workflow.json"

            code, stdout, stderr = run_cli(["run", str(workflow_path), "--data-dir", str(data_dir)])
            self.assertEqual(code, 0, stderr)
            self.assertIn("Workflow execution:", stdout)
            self.assertIn("Pending approval:", stdout)
            self.assertIn("Status: WAITING_APPROVAL", stdout)

            workflow_execution_id = None
            for line in stdout.splitlines():
                if line.startswith("Workflow execution:"):
                    workflow_execution_id = line.split(":", 1)[1].strip()
            self.assertIsNotNone(workflow_execution_id)

            code, stdout, stderr = run_cli(["status", workflow_execution_id, "--data-dir", str(data_dir)])
            self.assertEqual(code, 0, stderr)
            self.assertIn("Status: WAITING_APPROVAL", stdout)
            self.assertIn("Pending approval: human_operator", stdout)

            code, stdout, stderr = run_cli(["approve", workflow_execution_id, "--data-dir", str(data_dir), "--approved-by", "qa-lead", "--comment", "Looks good."])
            self.assertEqual(code, 0, stderr)
            self.assertIn("Approval recorded for workflow execution", stdout)

            code, stdout, stderr = run_cli(["resume", workflow_execution_id, "--data-dir", str(data_dir)])
            self.assertEqual(code, 0, stderr)
            self.assertIn("Status: COMPLETED", stdout)
            self.assertIn("Produced artifacts:", stdout)
            self.assertIn("Tool activity:", stdout)

            code, stdout, stderr = run_cli(["list", "workflows", "--data-dir", str(data_dir)])
            self.assertEqual(code, 0, stderr)
            self.assertIn("Persisted workflow definitions:", stdout)

            code, stdout, stderr = run_cli(["list", "executions", "--data-dir", str(data_dir)])
            self.assertEqual(code, 0, stderr)
            self.assertIn("Persisted workflow executions:", stdout)

            code, stdout, stderr = run_cli(["list", "tools", "--data-dir", str(data_dir)])
            self.assertEqual(code, 0, stderr)
            self.assertIn("Persisted tools:", stdout)


if __name__ == "__main__":
    unittest.main()
