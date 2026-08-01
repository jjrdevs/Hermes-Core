import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from engine.models import WorkflowDefinition
from engine.runtime_service import RuntimeService
from engine.scheduler import BackgroundScheduler, SQLiteJobStore
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

    def test_status_command_surfaces_run_summary_for_approval_workflows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            workflow_path = Path(__file__).resolve().parent.parent / "examples" / "approval_example.json"

            code, stdout, stderr = run_cli(["run", str(workflow_path), "--data-dir", str(data_dir)])
            self.assertEqual(code, 0, stderr)
            workflow_execution_id = None
            for line in stdout.splitlines():
                if line.startswith("Workflow execution:"):
                    workflow_execution_id = line.split(":", 1)[1].strip()
            self.assertIsNotNone(workflow_execution_id)

            code, stdout, stderr = run_cli(["status", workflow_execution_id, "--data-dir", str(data_dir)])
            self.assertEqual(code, 0, stderr)
            self.assertIn("Pending approval:", stdout)
            self.assertIn("Artifacts:", stdout)

    def test_status_command_surfaces_latest_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
            service = RuntimeService(data_dir=str(data_dir))
            try:
                service.remember(
                    "lesson",
                    "Prefer explicit verification markers for new repos.",
                    topic=str(data_dir),
                    source="test",
                    metadata={"task": "validate"},
                )
                result = service.run_task(
                    "inspect the repository",
                    workspace_path=str(data_dir),
                    context={"max_iterations": 1, "provider": "stub"},
                )
                self.assertIn("checkpoint_id", result)

                code, stdout, stderr = run_cli(["run", str(workflow_path), "--data-dir", str(data_dir)])
                self.assertEqual(code, 0, stderr)

                workflow_execution_id = None
                for line in stdout.splitlines():
                    if line.startswith("Workflow execution:"):
                        workflow_execution_id = line.split(":", 1)[1].strip()
                self.assertIsNotNone(workflow_execution_id)

                code, stdout, stderr = run_cli(["status", workflow_execution_id, "--data-dir", str(data_dir)])
                self.assertEqual(code, 0, stderr)
                self.assertIn("Latest checkpoint:", stdout)
                self.assertIn(result["checkpoint_id"], stdout)
                self.assertIn(result["summary"]["execution_loop"]["stop_reason"], stdout)
                self.assertIn("provider=stub", stdout)
                self.assertIn("memory_count=1", stdout)
            finally:
                service.shutdown()

    def test_run_command_supports_a_dry_run_preview(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"

            code, stdout, stderr = run_cli(["run", str(workflow_path), "--dry-run", "--data-dir", str(data_dir)])
            self.assertEqual(code, 0, stderr)
            self.assertIn("Dry run", stdout)
            self.assertIn("hello_world", stdout)

    def test_checkpoint_commands_list_and_get_persisted_checkpoints(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            service = RuntimeService(data_dir=str(data_dir))
            try:
                result = service.run_task("inspect the repository", workspace_path=str(data_dir))
                self.assertIn("checkpoint_id", result)

                code, stdout, stderr = run_cli(["checkpoint", "list", "--data-dir", str(data_dir)])
                self.assertEqual(code, 0, stderr)
                self.assertIn(result["checkpoint_id"], stdout)
                self.assertIn("phase=", stdout)
                self.assertIn("stop_reason=", stdout)

                code, stdout, stderr = run_cli(["checkpoint", "get", result["checkpoint_id"], "--data-dir", str(data_dir)])
                self.assertEqual(code, 0, stderr)
                self.assertIn(result["checkpoint_id"], stdout)
                self.assertIn("phase=plan", stdout)
                self.assertIn("provider=stub", stdout)
            finally:
                service.shutdown()

    def test_checkpoint_get_command_renders_a_compact_progress_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            service = RuntimeService(data_dir=str(data_dir))
            try:
                result = service.run_task(
                    "inspect the repository",
                    workspace_path=str(data_dir),
                    context={"max_iterations": 1, "provider": "stub"},
                )

                code, stdout, stderr = run_cli(["checkpoint", "get", result["checkpoint_id"], "--data-dir", str(data_dir)])
                self.assertEqual(code, 0, stderr)
                self.assertIn("Checkpoint:", stdout)
                self.assertIn("phase=plan", stdout)
                self.assertIn("status=completed", stdout)
                self.assertIn("provider=stub", stdout)
            finally:
                service.shutdown()

    def test_rollback_command_restores_checkpointed_edit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            target_path = workspace / "notes.txt"
            target_path.write_text("original\n", encoding="utf-8")
            service = RuntimeService(data_dir=str(data_dir))
            try:
                result = service.run_task("Record a maintenance note.", workspace_path=str(workspace))
                self.assertNotEqual(target_path.read_text(encoding="utf-8"), "original\n")

                code, stdout, stderr = run_cli(["rollback", result["checkpoint_id"], "--data-dir", str(data_dir)])

                self.assertEqual(code, 0, stderr)
                self.assertIn("Rolled back", stdout)
                self.assertEqual(target_path.read_text(encoding="utf-8"), "original\n")
            finally:
                service.shutdown()

    def test_change_get_command_renders_patch_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            (workspace / "README.md").write_text("before\n", encoding="utf-8")
            service = RuntimeService(data_dir=str(data_dir))
            try:
                result = service.run_task(
                    "Update the README.",
                    workspace_path=str(workspace),
                    context={"proposed_patch": {"path": "README.md", "content": "after\n"}},
                )

                code, stdout, stderr = run_cli(["change", "get", result["checkpoint_id"], "--data-dir", str(data_dir)])

                self.assertEqual(code, 0, stderr)
                self.assertIn("Change record:", stdout)
                self.assertIn("before_hash=", stdout)
                self.assertIn("after_hash=", stdout)
                self.assertIn("+after", stdout)
            finally:
                service.shutdown()

    def test_policy_denials_command_handles_empty_store(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            code, stdout, stderr = run_cli(["policy", "denials", "--data-dir", temp_dir])
            self.assertEqual(code, 0, stderr)
            self.assertIn("No policy denials", stdout)

    def test_job_list_command_surfaces_execution_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            job_store = SQLiteJobStore(data_dir / "jobs.db")
            try:
                scheduler = BackgroundScheduler(job_store)
                job = scheduler.create_job("Inspect the repository", schedule="manual", runtime_budget_seconds=30)
                scheduler.start_job(
                    job["job_id"],
                    executor=lambda payload: {
                        "status": "completed",
                        "summary": "done",
                        "checkpoint": "checkpoint-7",
                        "resume_hint": {"can_resume": True, "resume_target": "checkpoint-7"},
                    },
                )

                code, stdout, stderr = run_cli(["job", "list", "--data-dir", str(data_dir)])
                self.assertEqual(code, 0, stderr)
                self.assertIn("checkpoint-7", stdout)
                self.assertIn("resume=true", stdout.lower())
            finally:
                job_store.close()

    def test_job_get_command_prints_detailed_job_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            job_store = SQLiteJobStore(data_dir / "jobs.db")
            try:
                scheduler = BackgroundScheduler(job_store)
                job = scheduler.create_job("Inspect the repository", schedule="manual", runtime_budget_seconds=30)
                scheduler.start_job(
                    job["job_id"],
                    executor=lambda payload: {
                        "status": "completed",
                        "summary": "done",
                        "checkpoint": "checkpoint-8",
                        "resume_hint": {"can_resume": True, "resume_target": "checkpoint-8"},
                    },
                )

                code, stdout, stderr = run_cli(["job", "get", job["job_id"], "--data-dir", str(data_dir)])
                self.assertEqual(code, 0, stderr)
                self.assertIn(job["job_id"], stdout)
                self.assertIn("checkpoint-8", stdout)
                self.assertIn("resume=true", stdout.lower())
            finally:
                job_store.close()

    def test_job_resume_command_reuses_the_latest_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            job_store = SQLiteJobStore(data_dir / "jobs.db")
            try:
                scheduler = BackgroundScheduler(job_store)
                job = scheduler.create_job("Inspect the repository", schedule="manual", runtime_budget_seconds=30)
                scheduler.start_job(
                    job["job_id"],
                    executor=lambda payload: {
                        "status": "completed",
                        "summary": "done",
                        "checkpoint": "checkpoint-9",
                        "resume_hint": {"can_resume": True, "resume_target": "checkpoint-9"},
                    },
                )

                code, stdout, stderr = run_cli(["job", "resume", job["job_id"], "--data-dir", str(data_dir)])
                self.assertEqual(code, 0, stderr)
                self.assertIn(job["job_id"], stdout)
                self.assertIn("Resumed", stdout)
            finally:
                job_store.close()

    def test_memory_list_command_surfaces_persisted_memories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            service = RuntimeService(data_dir=str(data_dir))
            try:
                service.remember(
                    "lesson",
                    "Prefer explicit verification markers for new repos.",
                    topic=str(data_dir),
                    source="test",
                    metadata={"task": "validate"},
                )

                code, stdout, stderr = run_cli(["memory", "list", "--data-dir", str(data_dir)])
                self.assertEqual(code, 0, stderr)
                self.assertIn("Prefer explicit verification markers for new repos.", stdout)
            finally:
                service.shutdown()

    def test_memory_add_command_persists_a_new_memory_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            code, stdout, stderr = run_cli([
                "memory",
                "add",
                "lesson",
                "Prefer explicit verification markers for new repos.",
                "--topic",
                str(data_dir),
                "--source",
                "test",
                "--metadata",
                '{"task":"validate"}',
                "--data-dir",
                str(data_dir),
            ])
            self.assertEqual(code, 0, stderr)
            self.assertIn("Stored memory", stdout)

    def test_memory_recall_command_filters_memories_by_query(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            service = RuntimeService(data_dir=str(data_dir))
            try:
                service.remember(
                    "lesson",
                    "Prefer explicit verification markers for new repos.",
                    topic=str(data_dir),
                    source="test",
                    metadata={"task": "validate"},
                )
                service.remember(
                    "lesson",
                    "Ignore weather reports while working on code.",
                    topic=str(data_dir),
                    source="test",
                )

                code, stdout, stderr = run_cli([
                    "memory",
                    "recall",
                    "verification",
                    "--topic",
                    str(data_dir),
                    "--data-dir",
                    str(data_dir),
                ])
                self.assertEqual(code, 0, stderr)
                self.assertIn("Prefer explicit verification markers for new repos.", stdout)
                self.assertNotIn("Ignore weather reports while working on code.", stdout)
            finally:
                service.shutdown()


if __name__ == "__main__":
    unittest.main()
