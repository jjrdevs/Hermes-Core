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


class TestHermesCLI(unittest.TestCase):
    def test_validate_command_accepts_valid_workflow(self):
        path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
        code, stdout, stderr = run_cli(["validate", str(path)])
        self.assertEqual(code, 0)
        self.assertIn("is valid", stdout)
        self.assertEqual(stderr, "")

    def test_run_and_resume_workflow_persists_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            workflow_path = Path(__file__).resolve().parent.parent / "examples" / "two_step_pipeline.json"

            code, stdout, stderr = run_cli(["run", str(workflow_path), "--data-dir", str(data_dir)])
            self.assertEqual(code, 0, stderr)
            self.assertIn("Workflow execution:", stdout)

            event_db = data_dir / "events.db"
            self.assertTrue(event_db.exists())

            workflow_execution_id = None
            for line in stdout.splitlines():
                if line.startswith("Workflow execution:"):
                    workflow_execution_id = line.split(":", 1)[1].strip()
            self.assertIsNotNone(workflow_execution_id)

            code, stdout, stderr = run_cli(["status", workflow_execution_id, "--data-dir", str(data_dir)])
            self.assertEqual(code, 0, stderr)
            self.assertIn("State:", stdout)

            code, stdout, stderr = run_cli(["artifacts", workflow_execution_id, "--data-dir", str(data_dir)])
            self.assertEqual(code, 0, stderr)
            self.assertIn("artifact_id", stdout)

            code, stdout, stderr = run_cli(["resume", workflow_execution_id, "--data-dir", str(data_dir)])
            self.assertEqual(code, 0, stderr)
            self.assertIn("State:", stdout)

    def test_run_workflow_with_tool_registry_and_resume(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            workflow_path = Path(__file__).resolve().parent.parent / "examples" / "tool_example.json"

            code, stdout, stderr = run_cli(["run", str(workflow_path), "--data-dir", str(data_dir)])
            self.assertEqual(code, 0, stderr)
            self.assertIn("Workflow execution:", stdout)

            workflow_execution_id = None
            for line in stdout.splitlines():
                if line.startswith("Workflow execution:"):
                    workflow_execution_id = line.split(":", 1)[1].strip()
            self.assertIsNotNone(workflow_execution_id)

            code, stdout, stderr = run_cli(["status", workflow_execution_id, "--data-dir", str(data_dir)])
            self.assertEqual(code, 0, stderr)
            self.assertIn("State:", stdout)

            code, stdout, stderr = run_cli(["artifacts", workflow_execution_id, "--data-dir", str(data_dir)])
            self.assertEqual(code, 0, stderr)
            self.assertIn("artifact_id", stdout)

            code, stdout, stderr = run_cli(["resume", workflow_execution_id, "--data-dir", str(data_dir)])
            self.assertEqual(code, 0, stderr)
            self.assertIn("State:", stdout)


if __name__ == "__main__":
    unittest.main()
