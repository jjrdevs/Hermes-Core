import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from engine.models import WorkflowDefinition
from hermes_cli import resolve_data_paths, resolve_input_path

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
    def test_resolve_data_paths_uses_home_directory_when_not_overridden(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            original_cwd = Path.cwd()
            os.chdir(temp_dir)
            try:
                paths = resolve_data_paths(None)
            finally:
                os.chdir(original_cwd)

            expected_root = Path.home() / ".hermes" / "data"
            self.assertEqual(paths["event_db"], expected_root / "events.db")
            self.assertEqual(paths["artifact_db"], expected_root / "artifacts.db")
            self.assertEqual(paths["workflow_definition_db"], expected_root / "workflow_definitions.db")

    def test_resolve_input_path_finds_repo_workflow_when_cwd_changes(self):
        expected = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
        with tempfile.TemporaryDirectory() as temp_dir:
            original_cwd = Path.cwd()
            os.chdir(temp_dir)
            try:
                resolved = resolve_input_path("examples/hello_world.json")
            finally:
                os.chdir(original_cwd)

            self.assertEqual(resolved, expected)

    def test_resolve_input_path_finds_bundled_workflow_when_meipass_present(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundled_root = Path(temp_dir)
            bundled_examples = bundled_root / "examples"
            bundled_examples.mkdir(parents=True, exist_ok=True)
            expected = bundled_examples / "hello_world.json"
            expected.write_text('{"name": "bundled"}', encoding="utf-8")

            previous_meipass = getattr(sys, "_MEIPASS", None)
            sys._MEIPASS = str(bundled_root)
            try:
                resolved = resolve_input_path("examples/hello_world.json")
            finally:
                if previous_meipass is None:
                    del sys._MEIPASS
                else:
                    sys._MEIPASS = previous_meipass

            self.assertEqual(resolved, expected)

    def test_missing_workflow_path_raises_helpful_error(self):
        with self.assertRaises(FileNotFoundError) as context:
            resolve_input_path("does_not_exist.json")
        self.assertIn("Workflow file not found", str(context.exception))

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
