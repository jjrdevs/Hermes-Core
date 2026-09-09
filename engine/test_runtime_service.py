import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

from engine.models import TaskEnvelope
from engine.policy import PolicyEvaluator
from engine.runtime_service import RuntimeService
from engine.storage import SQLiteMemoryStore


class TestRuntimeService(unittest.TestCase):
    def test_shared_policy_gate_rejects_missing_policy_context_for_sensitive_tool_requests(self):
        evaluator = PolicyEvaluator()

        decision = evaluator.evaluate_tool_request(
            tool_id="shell",
            tool_action="run",
            policy_context={"approved": True, "policy_ids": ["maintenance"]},
            step_constraints={"allow_execution": True, "allowed_tools": ["shell"]},
            tool_constraints={"command": "pytest -q", "allowed_commands": ["pytest"]},
            strict_policy_context=True,
            extra_context={"tool_risk_level": "destructive"},
        )
        self.assertTrue(decision.allowed)

        denied = evaluator.evaluate_tool_request(
            tool_id="shell",
            tool_action="run",
            policy_context=None,
            step_constraints={"allow_execution": True, "allowed_tools": ["shell"]},
            tool_constraints={"command": "pytest -q", "allowed_commands": ["pytest"]},
            strict_policy_context=True,
            extra_context={"tool_risk_level": "destructive"},
        )
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason, "policy_context.missing")

    def test_shared_policy_gate_enforces_allowed_roots_from_capability_contract(self):
        evaluator = PolicyEvaluator()

        denied_shell = evaluator.evaluate_tool_request(
            tool_id="shell",
            tool_action="run",
            policy_context={"approved": True, "policy_ids": ["maintenance"]},
            step_constraints={"allow_execution": True, "allowed_tools": ["shell"]},
            tool_constraints={"command": "python -c 'print(1)'", "cwd": "/tmp"},
            strict_policy_context=True,
            extra_context={"capability_contract": {"allowed_roots": ["/workspace"]}},
        )
        self.assertFalse(denied_shell.allowed)
        self.assertEqual(denied_shell.reason, "capability_contract.path_not_allowed")

        denied_filesystem = evaluator.evaluate_tool_request(
            tool_id="filesystem",
            tool_action="write_file",
            policy_context={"approved": True, "policy_ids": ["maintenance"]},
            step_constraints={"allow_execution": True, "allowed_tools": ["filesystem"]},
            tool_constraints={"path": "/tmp/outside.txt"},
            strict_policy_context=True,
            extra_context={"capability_contract": {"allowed_roots": ["/workspace"]}},
        )
        self.assertFalse(denied_filesystem.allowed)
        self.assertEqual(denied_filesystem.reason, "capability_contract.path_not_allowed")

    def test_task_envelope_round_trips_capability_and_policy_contract(self):
        envelope = TaskEnvelope(
            objective={"description": "Inspect the repository", "success_criteria": ["Summarize findings"]},
            constraints={"dry_run": True, "allowed_tools": ["workspace_inspection"]},
            acceptance_criteria=["A summary artifact is written"],
            allowed_tools=["workspace_inspection"],
            budget={"limits": {"max_iterations": 1}, "usage": {"iterations": 0}},
            execution_context={"execution_mode": "read_only", "sandbox_profile": "strict"},
            policy_context={"approved": True, "policy_ids": ["read-only"]},
            capability_requirements=["workspace_inspection"],
        )

        payload = envelope.to_dict()
        restored = TaskEnvelope.from_dict(payload)

        self.assertEqual(restored.objective["description"], "Inspect the repository")
        self.assertEqual(restored.capability_requirements, ["workspace_inspection"])
        self.assertEqual(restored.execution_context["sandbox_profile"], "strict")
        self.assertEqual(restored.policy_context["policy_ids"], ["read-only"])

    def test_service_can_run_and_report_workflow_state(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_workflow(str(workflow_path))

                self.assertEqual(result["workflow_name"], "hello_world")
                self.assertEqual(result["status"], "COMPLETED")
                self.assertEqual(result["completed_steps"], ["say_hello"])
                self.assertGreaterEqual(len(result["artifacts"]), 1)

                summary = service.get_execution_summary(result["execution_id"])
                self.assertEqual(summary["status"], "COMPLETED")
                self.assertEqual(summary["workflow_name"], "hello_world")

                artifacts = service.get_artifacts(result["execution_id"])
                self.assertTrue(artifacts)
            finally:
                service.shutdown()

    def test_run_task_creates_a_summary_and_verification_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Summarize the repository state for a small maintenance task.",
                    workspace_path=temp_dir,
                )

                self.assertEqual(result["status"], "COMPLETED")
                self.assertIn("summary", result)
                self.assertIn("verification", result)
                self.assertIn(result["verification"]["status"], {"passed", "not_run"})
                self.assertTrue(result["artifacts"])
            finally:
                service.shutdown()

    def test_run_task_persists_and_resumes_from_a_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                first = service.run_task(
                    "Inspect the repository and summarize the next maintenance step.",
                    workspace_path=temp_dir,
                    context={"max_iterations": 1},
                )
                self.assertIn("checkpoint_id", first)
                self.assertEqual(first["checkpoint"]["iteration"], 1)
                self.assertEqual(first["status"], "IN_PROGRESS")

                resumed = service.run_task(
                    "Inspect the repository and summarize the next maintenance step.",
                    workspace_path=temp_dir,
                    context={"resume_from": first["checkpoint_id"], "max_iterations": 2},
                )
                self.assertEqual(resumed["status"], "COMPLETED")
                self.assertEqual(resumed["checkpoint"]["iteration"], 2)
                self.assertGreaterEqual(len(resumed["checkpoint"]["history"]), 2)
            finally:
                service.shutdown()

    def test_run_task_exposes_task_envelope_and_dry_run_preview(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Prepare a small maintenance task.",
                    workspace_path=temp_dir,
                    context={"dry_run": True, "allowed_tools": ["workspace_inspection", "artifact_creation"]},
                )

                self.assertEqual(result["status"], "COMPLETED")
                self.assertTrue(result["dry_run"])
                self.assertIn("task_envelope", result["summary"])
                self.assertEqual(result["summary"]["task_envelope"]["objective"]["description"], "Prepare a small maintenance task.")
                self.assertEqual(result["summary"]["task_envelope"]["constraints"]["dry_run"], True)
                self.assertTrue(result["summary"]["preview_actions"])
            finally:
                service.shutdown()

    def test_run_task_enforces_patch_budget_before_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            target_path = workspace / "README.md"
            target_path.write_text("before\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Update the README.",
                    workspace_path=str(workspace),
                    context={
                        "patch_proposal": {"path": "README.md", "content": "after\n"},
                        "max_patch_bytes": 3,
                    },
                )

                self.assertEqual(result["summary"]["patch_result"]["reason"], "patch_budget_exceeded")
                self.assertEqual(result["summary"]["budget"]["usage"]["patch_files"], 1)
                self.assertEqual(target_path.read_text(encoding="utf-8"), "before\n")
            finally:
                service.shutdown()

    def test_run_task_enforces_wall_time_budget_before_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            target_path = workspace / "README.md"
            target_path.write_text("before\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Update the README.",
                    workspace_path=str(workspace),
                    context={
                        "patch_proposal": {"path": "README.md", "content": "after\n"},
                        "max_wall_time_seconds": 0,
                    },
                )

                self.assertEqual(result["summary"]["patch_result"]["reason"], "wall_time_budget_exceeded")
                self.assertEqual(result["summary"]["task_status"], "PARTIAL")
                self.assertEqual(target_path.read_text(encoding="utf-8"), "before\n")
            finally:
                service.shutdown()

    def test_run_task_enforces_patch_file_count_budget(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Update repository docs.",
                    workspace_path=str(workspace),
                    context={
                        "patch_proposal": [
                            {"path": "README.md", "content": "readme\n"},
                            {"path": "CHANGELOG.md", "content": "changelog\n"},
                        ],
                        "max_patch_files": 1,
                    },
                )

                self.assertEqual(result["summary"]["patch_result"]["reason"], "patch_budget_exceeded")
                self.assertEqual(result["summary"]["budget"]["usage"]["patch_files"], 2)
                self.assertFalse((workspace / "README.md").exists())
                self.assertFalse((workspace / "CHANGELOG.md").exists())
            finally:
                service.shutdown()

    def test_run_task_accumulates_patch_budget_usage_across_corrective_patch_attempts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            service = RuntimeService(data_dir=temp_dir)

            class FakeAdapter:
                def __init__(self) -> None:
                    self.calls = 0

                def generate(self, prompt, max_tokens=None):
                    self.calls += 1
                    if self.calls == 1:
                        return [{"path": "README.md", "content": "initial\n"}]
                    return [{"path": "README.md", "content": "correction\n"}]

            fake_adapter = FakeAdapter()
            fake_router = SimpleNamespace(provider="stub", fallback_used=False, reason="test", adapter=fake_adapter, health_summary={})
            try:
                with mock.patch.object(service, "_route_provider", return_value=fake_router):
                    result = service.run_task(
                        "Update the README with a corrective patch.",
                        workspace_path=str(workspace),
                        context={
                            "generate_patch": True,
                            "verification_command": ["python", "-c", "import sys; sys.exit(1)"],
                            "max_patch_files": 1,
                            "max_patch_bytes": 1024,
                            "max_corrections": 1,
                        },
                    )

                self.assertEqual(result["summary"]["patch_result"]["reason"], "patch_budget_exceeded")
                self.assertEqual(result["summary"]["budget"]["usage"]["patch_files"], 2)
                self.assertEqual(result["summary"]["budget"]["usage"]["patch_bytes"], len("initial\n".encode("utf-8")) + len("correction\n".encode("utf-8")))
            finally:
                service.shutdown()

    def test_run_task_exposes_compatibility_task_status_and_transitions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Inspect the repository.",
                    workspace_path=str(workspace),
                    context={"dry_run": True},
                )

                summary = result["summary"]
                self.assertEqual(result["status"], "COMPLETED")
                self.assertEqual(summary["task_status"], "COMPLETED_UNVERIFIED")
                self.assertEqual(result["task_status"], "COMPLETED_UNVERIFIED")
                self.assertEqual(result["status_schema_version"], 1)
                self.assertEqual(summary["status_schema_version"], 1)
                self.assertEqual(
                    [transition["to"] for transition in summary["task_transitions"]],
                    ["PLANNING", "VERIFYING", "COMPLETED_UNVERIFIED"],
                )
                checkpoint = service.get_checkpoint(result["checkpoint_id"])
                self.assertEqual(checkpoint["summary"]["task_status"], "COMPLETED_UNVERIFIED")
            finally:
                service.shutdown()

    def test_run_task_previews_a_structured_patch_without_mutating(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            target_path = workspace / "README.md"
            target_path.write_text("before\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Update the README.",
                    workspace_path=str(workspace),
                    context={
                        "dry_run": True,
                        "proposed_patch": {"path": "README.md", "content": "after\n"},
                    },
                )

                patch_result = result["summary"]["patch_result"]
                self.assertFalse(patch_result["applied"])
                self.assertEqual(patch_result["reason"], "dry_run")
                self.assertIn("before", patch_result["diff"])
                self.assertIn("after", patch_result["diff"])
                self.assertEqual(target_path.read_text(encoding="utf-8"), "before\n")
            finally:
                service.shutdown()

    def test_run_task_builds_a_structured_repository_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            (workspace / "README.md").write_text("hello\n", encoding="utf-8")
            (workspace / "src").mkdir()
            (workspace / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Update the README.",
                    workspace_path=str(workspace),
                    context={"dry_run": True},
                )

                self.assertIn("repository_plan", result["summary"])
                plan = result["summary"]["repository_plan"]
                self.assertEqual(plan["workspace_path"], str(workspace))
                self.assertIn("README.md", plan["candidate_files"])
                self.assertTrue(plan["plan"])
            finally:
                service.shutdown()

    def test_run_task_bounds_repository_inspection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            for index in range(5):
                (workspace / f"file-{index}.txt").write_text("content\n", encoding="utf-8")
            nested = workspace / "nested" / "deeper"
            nested.mkdir(parents=True)
            (nested / "deep.txt").write_text("deep\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Inspect the repository.",
                    workspace_path=str(workspace),
                    context={"dry_run": True, "max_inspection_files": 2, "max_inspection_depth": 1},
                )

                inspection = result["summary"]["workspace"]
                self.assertLessEqual(len(inspection["files"]), 2)
                self.assertEqual(inspection["inspection_limits"]["max_depth"], 1)
                self.assertEqual(inspection["inspection_limits"]["max_files"], 2)
                self.assertIn("bytes_read", inspection["inspection_usage"])
                self.assertNotIn("deep.txt", inspection["files"])
            finally:
                service.shutdown()

    def test_run_task_includes_bounded_text_previews_for_inspected_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            (workspace / "README.md").write_text("# Hermes\nInstall with pytest.\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Inspect the repository.",
                    workspace_path=str(workspace),
                    context={"dry_run": True, "max_inspection_files": 2, "max_inspection_bytes": 128},
                )

                previews = result["summary"]["workspace"]["file_previews"]
                self.assertIn("README.md", previews)
                self.assertIn("# Hermes", previews["README.md"])
                self.assertLessEqual(sum(len(content.encode("utf-8")) for content in previews.values()), 128)
            finally:
                service.shutdown()

    def test_run_task_records_normalized_patch_proposal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Create a README.",
                    workspace_path=str(workspace),
                    context={
                        "dry_run": True,
                        "patch_proposal": {"path": "README.md", "content": "hello\n"},
                    },
                )

                proposal = result["summary"]["patch_proposal"]
                self.assertEqual(proposal["status"], "proposed")
                self.assertEqual(proposal["patches"][0]["path"], "README.md")
                self.assertFalse((workspace / "README.md").exists())
            finally:
                service.shutdown()

    def test_run_task_rejects_invalid_patch_proposal_before_apply(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Apply a malformed proposal.",
                    workspace_path=str(workspace),
                    context={"patch_proposal": {"path": "README.md"}},
                )

                self.assertEqual(result["summary"]["patch_proposal"]["status"], "invalid")
                self.assertEqual(result["summary"]["patch_result"]["reason"], "invalid_patch_proposal")
                self.assertFalse((workspace / "README.md").exists())
            finally:
                service.shutdown()

    def test_run_task_accepts_strict_json_planner_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Create a README.",
                    workspace_path=str(workspace),
                    context={
                        "dry_run": True,
                        "planner_output": '{"path": "README.md", "content": "planned\\n"}',
                    },
                )

                proposal = result["summary"]["patch_proposal"]
                self.assertEqual(proposal["status"], "proposed")
                self.assertEqual(proposal["source"], "planner_output")
                self.assertIn("planned", result["summary"]["patch_result"]["diff"])
                self.assertFalse((workspace / "README.md").exists())
            finally:
                service.shutdown()

    def test_run_task_can_generate_a_patch_from_the_selected_adapter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            service = RuntimeService(data_dir=temp_dir)
            fake_adapter = SimpleNamespace(
                generate=mock.Mock(return_value='{"path": "README.md", "content": "generated\\n"}')
            )
            fake_routing = SimpleNamespace(provider="fake", fallback_used=False, reason="test", adapter=fake_adapter)
            try:
                with mock.patch.object(service, "_route_provider", return_value=fake_routing):
                    result = service.run_task(
                        "Create a README.",
                        workspace_path=str(workspace),
                        context={"dry_run": True, "generate_patch": True},
                    )

                self.assertEqual(result["summary"]["patch_proposal"]["source"], "planner_output")
                self.assertEqual(result["summary"]["patch_proposal"]["status"], "proposed")
                fake_adapter.generate.assert_called_once()
                self.assertFalse((workspace / "README.md").exists())
            finally:
                service.shutdown()

    def test_run_task_performs_one_bounded_correction_after_verification_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            tests_dir = workspace / "tests"
            tests_dir.mkdir(parents=True)
            (workspace / "README.md").write_text("before\n", encoding="utf-8")
            (tests_dir / "test_readme.py").write_text(
                "from pathlib import Path\n\ndef test_readme_updated():\n    assert Path('README.md').read_text() == 'corrected\\n'\n",
                encoding="utf-8",
            )
            (workspace / "Makefile").write_text("test:\n\tpytest -q tests/test_readme.py\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            fake_adapter = SimpleNamespace(
                generate=mock.Mock(side_effect=[
                    '{"path": "README.md", "content": "wrong\\n"}',
                    '{"path": "README.md", "content": "corrected\\n"}',
                ])
            )
            fake_routing = SimpleNamespace(provider="fake", fallback_used=False, reason="test", adapter=fake_adapter)
            try:
                with mock.patch.object(service, "_route_provider", return_value=fake_routing):
                    result = service.run_task(
                        "Update the README.",
                        workspace_path=str(workspace),
                        context={"generate_patch": True, "max_corrections": 1},
                    )

                self.assertEqual(result["summary"]["pre_verification"]["status"], "failed")
                self.assertEqual(result["summary"]["verification"]["status"], "passed")
                self.assertEqual(len(result["summary"]["corrections"]), 1)
                self.assertEqual((workspace / "README.md").read_text(encoding="utf-8"), "corrected\n")
                self.assertEqual(fake_adapter.generate.call_count, 2)
            finally:
                service.shutdown()

    def test_run_task_performs_one_default_correction_after_verification_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            tests_dir = workspace / "tests"
            tests_dir.mkdir(parents=True)
            (workspace / "README.md").write_text("before\n", encoding="utf-8")
            (tests_dir / "test_readme.py").write_text(
                "from pathlib import Path\n\ndef test_readme_updated():\n    assert Path('README.md').read_text() == 'corrected\\n'\n",
                encoding="utf-8",
            )
            (workspace / "Makefile").write_text("test:\n\tpytest -q tests/test_readme.py\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            fake_adapter = SimpleNamespace(
                generate=mock.Mock(side_effect=[
                    '{"path": "README.md", "content": "wrong\\n"}',
                    '{"path": "README.md", "content": "corrected\\n"}',
                ])
            )
            fake_routing = SimpleNamespace(provider="fake", fallback_used=False, reason="test", adapter=fake_adapter)
            try:
                with mock.patch.object(service, "_route_provider", return_value=fake_routing):
                    result = service.run_task(
                        "Update the README.",
                        workspace_path=str(workspace),
                        context={"generate_patch": True},
                    )

                self.assertEqual(result["summary"]["verification"]["status"], "passed")
                self.assertEqual(len(result["summary"]["corrections"]), 1)
                self.assertEqual((workspace / "README.md").read_text(encoding="utf-8"), "corrected\n")
            finally:
                service.shutdown()

    def test_run_task_enforces_patch_budget_on_correction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "tests").mkdir()
            (workspace / "tests" / "test_readme.py").write_text("def test_readme():\n    assert False\n", encoding="utf-8")
            (workspace / "README.md").write_text("before\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            fake_adapter = SimpleNamespace(
                generate=mock.Mock(return_value='{"path": "README.md", "content": "this correction is too large\\n"}')
            )
            fake_routing = SimpleNamespace(provider="fake", fallback_used=False, reason="test", adapter=fake_adapter)
            try:
                with mock.patch.object(service, "_route_provider", return_value=fake_routing):
                    result = service.run_task(
                        "Correct the README.",
                        workspace_path=temp_dir,
                        context={"generate_patch": True, "max_corrections": 1, "max_patch_bytes": 3},
                    )

                correction_result = result["summary"]["corrections"][0]["patch_result"]
                self.assertEqual(correction_result["reason"], "patch_budget_exceeded")
                self.assertEqual((workspace / "README.md").read_text(encoding="utf-8"), "before\n")
            finally:
                service.shutdown()

    def test_run_task_rejects_non_json_planner_output_before_apply(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Create a README.",
                    workspace_path=str(workspace),
                    context={"planner_output": "Here is a patch: README.md"},
                )

                self.assertEqual(result["summary"]["patch_proposal"]["status"], "invalid")
                self.assertEqual(result["summary"]["patch_proposal"]["reason"], "planner_output_not_json")
                self.assertFalse((workspace / "README.md").exists())
            finally:
                service.shutdown()

    def test_run_task_applies_structured_patch_and_records_hashes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            target_path = workspace / "README.md"
            target_path.write_text("before\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Update the README.",
                    workspace_path=str(workspace),
                    context={
                        "proposed_patch": {"path": "README.md", "content": "after\n"},
                    },
                )

                patch_result = result["summary"]["patch_result"]
                self.assertTrue(patch_result["applied"])
                self.assertEqual(target_path.read_text(encoding="utf-8"), "after\n")
                self.assertNotEqual(patch_result["before_hash"], patch_result["after_hash"])
                self.assertEqual(result["summary"]["pre_verification"], result["summary"]["verification"])
            finally:
                service.shutdown()

    def test_run_task_reports_post_change_verification(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            tests_dir = workspace / "tests"
            tests_dir.mkdir(parents=True)
            (workspace / "README.md").write_text("before\n", encoding="utf-8")
            (tests_dir / "test_readme.py").write_text(
                "from pathlib import Path\n\ndef test_readme_updated():\n    assert Path('README.md').read_text() == 'after\\n'\n",
                encoding="utf-8",
            )
            (workspace / "Makefile").write_text("test:\n\tpytest -q tests/test_readme.py\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Update the README.",
                    workspace_path=str(workspace),
                    context={"proposed_patch": {"path": "README.md", "content": "after\n"}},
                )

                self.assertEqual(result["summary"]["pre_verification"]["status"], "failed")
                self.assertEqual(result["summary"]["verification"]["status"], "passed")
                self.assertEqual(result["summary"]["verification_report"]["status"], "passed")
            finally:
                service.shutdown()

    def test_run_task_rejects_structured_patch_outside_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            outside_path = Path(temp_dir) / "outside.txt"
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Attempt an unsafe patch.",
                    workspace_path=str(workspace),
                    context={"proposed_patch": {"path": "../outside.txt", "content": "blocked\n"}},
                )

                self.assertEqual(result["summary"]["patch_result"]["reason"], "patch_outside_workspace")
                self.assertFalse(outside_path.exists())
            finally:
                service.shutdown()

    def test_run_task_applies_and_rolls_back_a_multi_file_patch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            first_path = workspace / "README.md"
            second_path = workspace / "docs" / "notes.md"
            first_path.write_text("before readme\n", encoding="utf-8")
            second_path.parent.mkdir()
            second_path.write_text("before notes\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Update repository documentation.",
                    workspace_path=str(workspace),
                    context={
                        "proposed_patch": [
                            {"path": "README.md", "content": "after readme\n"},
                            {"path": "docs/notes.md", "content": "after notes\n"},
                        ]
                    },
                )

                patch_result = result["summary"]["patch_result"]
                self.assertTrue(patch_result["applied"])
                self.assertTrue(patch_result["change_id"])
                self.assertEqual(len(patch_result["changes"]), 2)
                self.assertEqual(first_path.read_text(encoding="utf-8"), "after readme\n")
                self.assertEqual(second_path.read_text(encoding="utf-8"), "after notes\n")

                rollback = service.rollback_checkpoint(result["checkpoint_id"], actor="test-user")

                self.assertEqual(rollback["status"], "ROLLED_BACK")
                self.assertEqual(first_path.read_text(encoding="utf-8"), "before readme\n")
                self.assertEqual(second_path.read_text(encoding="utf-8"), "before notes\n")
                self.assertEqual(len(rollback["paths"]), 2)
            finally:
                service.shutdown()

    def test_run_task_leaves_workspace_unchanged_when_a_multi_file_patch_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            first_path = workspace / "README.md"
            second_path = workspace / "docs"
            first_path.write_text("before readme\n", encoding="utf-8")
            second_path.mkdir()
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Attempt a conflicting patch.",
                    workspace_path=str(workspace),
                    context={
                        "proposed_patch": [
                            {"path": "README.md", "content": "after readme\n"},
                            {"path": "docs", "content": "not a file\n"},
                        ]
                    },
                )

                self.assertEqual(result["summary"]["patch_result"]["applied"], False)
                self.assertEqual(first_path.read_text(encoding="utf-8"), "before readme\n")
                self.assertTrue(second_path.exists())
                self.assertTrue(second_path.is_dir())
                self.assertEqual(result["summary"]["patch_result"]["reason"], "target_is_directory")
            finally:
                service.shutdown()

    def test_run_task_persists_rollback_metadata_for_multi_file_patch_transactions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            first_path = workspace / "README.md"
            second_path = workspace / "docs" / "notes.md"
            first_path.write_text("before readme\n", encoding="utf-8")
            second_path.parent.mkdir(parents=True)
            second_path.write_text("before notes\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Update repository docs.",
                    workspace_path=str(workspace),
                    context={
                        "proposed_patch": [
                            {"path": "README.md", "content": "after readme\n"},
                            {"path": "docs/notes.md", "content": "after notes\n"},
                        ]
                    },
                )

                patch_result = result["summary"]["patch_result"]
                self.assertTrue(patch_result["applied"])
                self.assertEqual(len(patch_result["changes"]), 2)
                self.assertIn("rollback", patch_result)
                self.assertEqual(patch_result["rollback"]["status"], "prepared")
                self.assertEqual(first_path.read_text(encoding="utf-8"), "after readme\n")
                self.assertEqual(second_path.read_text(encoding="utf-8"), "after notes\n")
            finally:
                service.shutdown()

    def test_multi_file_patch_rolls_back_when_a_staged_replacement_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            first_path = workspace / "README.md"
            second_path = workspace / "docs" / "notes.md"
            first_path.write_text("before readme\n", encoding="utf-8")
            second_path.parent.mkdir(parents=True)
            second_path.write_text("before notes\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                original_replace = __import__("os").replace
                replace_calls = 0

                def fail_on_second_replace(source, destination):
                    nonlocal replace_calls
                    replace_calls += 1
                    if replace_calls == 2:
                        raise OSError("injected replacement failure")
                    return original_replace(source, destination)

                with mock.patch("engine.runtime_service.os.replace", side_effect=fail_on_second_replace):
                    result = service.run_task(
                        "Update repository docs.",
                        workspace_path=str(workspace),
                        context={
                            "proposed_patch": [
                                {"path": "README.md", "content": "after readme\n"},
                                {"path": "docs/notes.md", "content": "after notes\n"},
                            ]
                        },
                    )

                self.assertFalse(result["summary"]["patch_result"]["applied"])
                self.assertEqual(result["summary"]["patch_result"]["reason"], "write_failed")
                self.assertEqual(first_path.read_text(encoding="utf-8"), "before readme\n")
                self.assertEqual(second_path.read_text(encoding="utf-8"), "before notes\n")
                self.assertEqual(list(workspace.rglob("*.tmp")), [])
            finally:
                service.shutdown()

    def test_multi_file_patch_aborts_when_staging_a_file_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            first_path = workspace / "README.md"
            second_path = workspace / "docs" / "notes.md"
            first_path.write_text("before readme\n", encoding="utf-8")
            second_path.parent.mkdir(parents=True)
            second_path.write_text("before notes\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                original_write_text = Path.write_text
                staging_writes = 0

                def fail_on_second_staging_write(path, data, *args, **kwargs):
                    nonlocal staging_writes
                    if str(path).endswith(".tmp"):
                        staging_writes += 1
                        if staging_writes == 2:
                            raise OSError("injected staging failure")
                    return original_write_text(path, data, *args, **kwargs)

                with mock.patch.object(Path, "write_text", new=fail_on_second_staging_write):
                    result = service.run_task(
                        "Update repository docs.",
                        workspace_path=str(workspace),
                        context={
                            "proposed_patch": [
                                {"path": "README.md", "content": "after readme\n"},
                                {"path": "docs/notes.md", "content": "after notes\n"},
                            ]
                        },
                    )

                self.assertFalse(result["summary"]["patch_result"]["applied"])
                self.assertEqual(result["summary"]["patch_result"]["reason"], "write_failed")
                self.assertEqual(first_path.read_text(encoding="utf-8"), "before readme\n")
                self.assertEqual(second_path.read_text(encoding="utf-8"), "before notes\n")
                self.assertEqual(list(workspace.rglob("*.tmp")), [])
            finally:
                service.shutdown()

    def test_checkpoint_write_is_atomic_and_change_record_has_stable_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Update the README.",
                    workspace_path=str(workspace),
                    context={"proposed_patch": {"path": "README.md", "content": "after\n"}},
                )
                record = service.get_change_record(result["checkpoint_id"])
                self.assertEqual(record["change_id"], result["summary"]["patch_result"]["change_id"])
                checkpoint_path = Path(temp_dir) / "task_checkpoints.json"
                self.assertTrue(checkpoint_path.exists())
                self.assertFalse(list(Path(temp_dir).glob("task_checkpoints.json.*.tmp")))
            finally:
                service.shutdown()

    def test_change_record_exposes_patch_hashes_diff_and_rollback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            target_path = workspace / "README.md"
            target_path.write_text("before\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Update the README.",
                    workspace_path=str(workspace),
                    context={"proposed_patch": {"path": "README.md", "content": "after\n"}},
                )

                record = service.get_change_record(result["checkpoint_id"])

                self.assertEqual(record["checkpoint_id"], result["checkpoint_id"])
                self.assertNotEqual(record["patch_result"]["before_hash"], record["patch_result"]["after_hash"])
                self.assertIn("after", record["patch_result"]["diff"])
                self.assertIn("verification", record)
                self.assertIsNone(record["rollback"])
            finally:
                service.shutdown()

    def test_run_task_fails_closed_in_strict_policy_mode_without_policy_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Update the README.",
                    workspace_path=str(workspace),
                    context={"dry_run": True, "require_policy_context": True},
                )

                self.assertEqual(result["summary"]["policy_context"]["approved"], False)
                self.assertEqual(result["summary"]["policy_context"]["reason"], "policy_context.missing")
                self.assertEqual(result["summary"]["patch_result"]["reason"], "policy_context.missing")
            finally:
                service.shutdown()

    def test_run_task_preserves_policy_context_when_resuming_a_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            service = RuntimeService(data_dir=temp_dir)
            try:
                first = service.run_task(
                    "Inspect the repository before editing.",
                    workspace_path=str(workspace),
                    context={
                        "dry_run": True,
                        "max_iterations": 1,
                        "require_policy_context": True,
                        "policy_context": {"approved": True, "policy_ids": ["maintenance-read"]},
                    },
                )
                resumed = service.run_task(
                    "Inspect the repository before editing.",
                    workspace_path=str(workspace),
                    context={
                        "dry_run": True,
                        "resume_from": first["checkpoint_id"],
                        "max_iterations": 2,
                        "require_policy_context": True,
                    },
                )

                self.assertTrue(resumed["summary"]["policy_context"]["approved"])
                self.assertEqual(resumed["summary"]["policy_context"]["policy_ids"], ["maintenance-read"])
                self.assertEqual(resumed["summary"]["patch_result"]["reason"], "dry_run")
            finally:
                service.shutdown()

    def test_run_task_rejects_resume_after_workspace_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            target_path = workspace / "README.md"
            target_path.write_text("before\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                first = service.run_task(
                    "Inspect the repository.",
                    workspace_path=str(workspace),
                    context={"dry_run": True, "max_iterations": 1},
                )
                target_path.write_text("changed externally\n", encoding="utf-8")

                resumed = service.run_task(
                    "Inspect the repository.",
                    workspace_path=str(workspace),
                    context={"dry_run": True, "resume_from": first["checkpoint_id"], "max_iterations": 2},
                )

                self.assertEqual(resumed["summary"]["task_status"], "WORKSPACE_CONFLICT")
                self.assertEqual(resumed["summary"]["stop_reason"], "workspace_changed_since_checkpoint")
            finally:
                service.shutdown()

    def test_run_task_persists_touched_file_hashes_in_checkpoint_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            (workspace / "README.md").write_text("before\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                first = service.run_task(
                    "Update the README.",
                    workspace_path=str(workspace),
                    context={"proposed_patch": {"path": "README.md", "content": "after\n"}},
                )
                checkpoint = service.get_checkpoint(first["checkpoint_id"])
                self.assertIn("README.md", checkpoint["workspace_identity"]["touched_file_hashes"])
                self.assertEqual(
                    checkpoint["workspace_identity"]["touched_file_hashes"]["README.md"],
                    checkpoint["workspace_snapshot"]["file_hashes"]["README.md"],
                )
            finally:
                service.shutdown()

    def test_run_task_allows_resume_when_only_an_unrelated_file_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            (workspace / "README.md").write_text("before\n", encoding="utf-8")
            (workspace / "notes.txt").write_text("keep me\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                first = service.run_task(
                    "Update the README.",
                    workspace_path=str(workspace),
                    context={
                        "max_iterations": 1,
                        "proposed_patch": {"path": "README.md", "content": "after\n"},
                    },
                )
                (workspace / "notes.txt").write_text("changed externally\n", encoding="utf-8")

                resumed = service.run_task(
                    "Update the README.",
                    workspace_path=str(workspace),
                    context={"dry_run": True, "resume_from": first["checkpoint_id"], "max_iterations": 2},
                )

                self.assertNotEqual(resumed["summary"]["task_status"], "WORKSPACE_CONFLICT")
                self.assertEqual(resumed["summary"]["patch_result"]["reason"], "dry_run")
            finally:
                service.shutdown()

    def test_run_task_rejects_resume_after_task_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            service = RuntimeService(data_dir=temp_dir)
            try:
                first = service.run_task(
                    "Inspect the repository.",
                    workspace_path=str(workspace),
                    context={"dry_run": True, "max_iterations": 1},
                )

                resumed = service.run_task(
                    "Apply an unrelated change.",
                    workspace_path=str(workspace),
                    context={"dry_run": True, "resume_from": first["checkpoint_id"], "max_iterations": 2},
                )

                self.assertEqual(resumed["summary"]["task_status"], "TASK_CONFLICT")
                self.assertEqual(resumed["summary"]["stop_reason"], "task_changed_since_checkpoint")
            finally:
                service.shutdown()

    def test_run_task_rejects_resume_after_policy_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            service = RuntimeService(data_dir=temp_dir)
            try:
                first = service.run_task(
                    "Inspect the repository.",
                    workspace_path=str(workspace),
                    context={
                        "dry_run": True,
                        "max_iterations": 1,
                        "require_policy_context": True,
                        "policy_context": {"approved": True, "policy_ids": ["read-only"]},
                    },
                )

                resumed = service.run_task(
                    "Inspect the repository.",
                    workspace_path=str(workspace),
                    context={
                        "dry_run": True,
                        "resume_from": first["checkpoint_id"],
                        "max_iterations": 2,
                        "require_policy_context": True,
                        "policy_context": {"approved": True, "policy_ids": ["different-policy"]},
                    },
                )

                self.assertEqual(resumed["summary"]["task_status"], "POLICY_CONFLICT")
                self.assertEqual(resumed["summary"]["stop_reason"], "policy_context_changed_since_checkpoint")
            finally:
                service.shutdown()

    def test_run_task_persists_checkpoint_schema_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Inspect the repository.",
                    workspace_path=temp_dir,
                    context={"dry_run": True},
                )

                checkpoint = service.get_checkpoint(result["checkpoint_id"])
                self.assertEqual(checkpoint["checkpoint_schema_version"], 1)
            finally:
                service.shutdown()

    def test_run_task_rejects_unsupported_checkpoint_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                first = service.run_task("Inspect the repository.", workspace_path=temp_dir, context={"dry_run": True})
                checkpoint = service.get_checkpoint(first["checkpoint_id"])
                checkpoint["checkpoint_schema_version"] = 99
                service._save_checkpoint(first["checkpoint_id"], checkpoint)

                resumed = service.run_task(
                    "Inspect the repository.",
                    workspace_path=temp_dir,
                    context={"dry_run": True, "resume_from": first["checkpoint_id"]},
                )

                self.assertEqual(resumed["summary"]["task_status"], "CHECKPOINT_CONFLICT")
                self.assertEqual(resumed["summary"]["stop_reason"], "unsupported_checkpoint_schema")
            finally:
                service.shutdown()

    def test_run_task_rejects_resume_after_task_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            service = RuntimeService(data_dir=temp_dir)
            try:
                first = service.run_task(
                    "Inspect the repository.",
                    workspace_path=str(workspace),
                    context={"dry_run": True, "max_iterations": 1},
                )

                resumed = service.run_task(
                    "Apply an unrelated change.",
                    workspace_path=str(workspace),
                    context={"dry_run": True, "resume_from": first["checkpoint_id"], "max_iterations": 2},
                )

                self.assertEqual(resumed["summary"]["task_status"], "TASK_CONFLICT")
                self.assertEqual(resumed["summary"]["stop_reason"], "task_changed_since_checkpoint")
            finally:
                service.shutdown()

    def test_run_task_rejects_resume_after_policy_context_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            service = RuntimeService(data_dir=temp_dir)
            try:
                first = service.run_task(
                    "Inspect the repository.",
                    workspace_path=str(workspace),
                    context={
                        "dry_run": True,
                        "max_iterations": 1,
                        "require_policy_context": True,
                        "policy_context": {"approved": True, "policy_ids": ["read-only"]},
                    },
                )

                resumed = service.run_task(
                    "Inspect the repository.",
                    workspace_path=str(workspace),
                    context={
                        "dry_run": True,
                        "resume_from": first["checkpoint_id"],
                        "max_iterations": 2,
                        "require_policy_context": True,
                        "policy_context": {"approved": True, "policy_ids": ["write-approved"]},
                    },
                )

                self.assertEqual(resumed["summary"]["task_status"], "POLICY_CONFLICT")
                self.assertEqual(resumed["summary"]["stop_reason"], "policy_context_changed_since_checkpoint")
            finally:
                service.shutdown()

    def test_policy_denial_query_is_read_only_and_scoped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                self.assertEqual(service.list_policy_denials(), [])
                self.assertEqual(service.list_policy_denials("missing-run"), [])
            finally:
                service.shutdown()

    def test_run_task_checkpoint_can_rollback_an_applied_edit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            target_path = workspace / "notes.txt"
            target_path.write_text("existing note\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Record a maintenance note.",
                    workspace_path=str(workspace),
                    context={"dry_run": False},
                )
                self.assertTrue(result["summary"]["patch_result"]["applied"])
                self.assertNotEqual(target_path.read_text(encoding="utf-8"), "existing note\n")

                rollback = service.rollback_checkpoint(result["checkpoint_id"], actor="test-user", reason="restore original note")

                self.assertEqual(rollback["status"], "ROLLED_BACK")
                self.assertEqual(rollback["actor"], "test-user")
                self.assertEqual(target_path.read_text(encoding="utf-8"), "existing note\n")
                checkpoint = service.get_checkpoint(result["checkpoint_id"])
                self.assertTrue(checkpoint["summary"]["patch_result"]["rolled_back"])
                self.assertEqual(checkpoint["rollback"]["reason"], "restore original note")
            finally:
                service.shutdown()

    def test_run_task_rollback_detects_changes_after_patch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            target_path = workspace / "README.md"
            target_path.write_text("before\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Update the README.",
                    workspace_path=str(workspace),
                    context={"proposed_patch": {"path": "README.md", "content": "after\n"}},
                )
                target_path.write_text("changed by someone else\n", encoding="utf-8")

                rollback = service.rollback_checkpoint(result["checkpoint_id"])

                self.assertEqual(rollback["status"], "CONFLICT")
                self.assertEqual(target_path.read_text(encoding="utf-8"), "changed by someone else\n")
            finally:
                service.shutdown()

    def test_run_task_executes_verification_and_retries_once_on_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "tests").mkdir()
            (workspace / "tests" / "test_sample.py").write_text("def test_sample():\n    assert False\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Run repository verification for the maintenance task.",
                    workspace_path=temp_dir,
                    context={"max_iterations": 1},
                )

                self.assertEqual(result["status"], "COMPLETED")
                self.assertEqual(result["summary"]["verification"]["status"], "failed")
                self.assertEqual(result["summary"]["task_status"], "FAILED_VERIFICATION")
                self.assertEqual(result["summary"]["recovery"]["attempts"], 2)
                self.assertTrue(result["summary"]["recovery"]["retried"])
            finally:
                service.shutdown()

    def test_run_task_respects_verification_attempt_budget(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "tests").mkdir()
            (workspace / "tests" / "test_sample.py").write_text("def test_sample():\n    assert False\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Run bounded repository verification.",
                    workspace_path=temp_dir,
                    context={"max_verification_attempts": 1},
                )

                self.assertEqual(result["summary"]["verification"]["status"], "failed")
                self.assertEqual(result["summary"]["recovery"]["attempts"], 1)
                self.assertFalse(result["summary"]["recovery"]["retried"])
                self.assertEqual(result["summary"]["budget"]["usage"]["verification_attempts"], 1)
            finally:
                service.shutdown()

    def test_run_task_bounds_verification_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            verification_script = workspace / "verify.py"
            verification_script.write_text("print('x' * 1000)\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Run bounded verification output.",
                    workspace_path=temp_dir,
                    context={
                        "verification_command": ["python", "verify.py"],
                        "max_output_bytes": 32,
                    },
                )

                self.assertEqual(result["summary"]["verification"]["status"], "failed")
                self.assertEqual(result["summary"]["verification"]["details"], "verification_output_exceeded")
                self.assertLessEqual(len(result["summary"]["verification"]["stdout"].encode("utf-8")), 32)
            finally:
                service.shutdown()

    def test_run_task_applies_wall_time_budget_to_verification(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            verification_script = workspace / "verify.py"
            verification_script.write_text("import time; time.sleep(2)\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Run time-bounded verification.",
                    workspace_path=temp_dir,
                    context={
                        "verification_command": ["python", "verify.py"],
                        "max_wall_time_seconds": 1,
                    },
                )

                self.assertEqual(result["summary"]["verification"]["status"], "failed")
                self.assertIn("timed out", result["summary"]["verification"]["details"].lower())
            finally:
                service.shutdown()

    def test_run_task_discovers_makefile_based_verification_commands(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "tests").mkdir()
            (workspace / "tests" / "test_sample.py").write_text("def test_sample():\n    assert True\n", encoding="utf-8")
            (workspace / "Makefile").write_text("test:\n\tpython -m pytest -q tests/test_sample.py\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Run the repository's configured verification command.",
                    workspace_path=temp_dir,
                )

                self.assertEqual(result["status"], "COMPLETED")
                self.assertEqual(result["summary"]["verification"]["command"][0], "make")
                self.assertEqual(result["summary"]["verification"]["command"][1], "test")
            finally:
                service.shutdown()

    def test_run_task_uses_explicit_workspace_scoped_verification_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "verify.py").write_text("print('explicit verification')\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Run the explicit repository verification command.",
                    workspace_path=temp_dir,
                    context={"verification_command": ["python", "verify.py"]},
                )

                self.assertEqual(result["summary"]["verification"]["status"], "passed")
                self.assertEqual(result["summary"]["verification"]["command"], ["python", "verify.py"])
                self.assertEqual(result["summary"]["task_status"], "COMPLETED_VERIFIED")
            finally:
                service.shutdown()

    def test_run_task_rejects_explicit_verification_command_outside_workspace(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Run an unsafe verification command.",
                    workspace_path=str(workspace),
                    context={"verification_command": ["python", "../verify.py"]},
                )

                self.assertEqual(result["summary"]["verification"]["status"], "failed")
                self.assertEqual(result["summary"]["verification"]["details"], "verification_command_outside_workspace")
            finally:
                service.shutdown()

    def test_run_task_rejects_unallowlisted_explicit_verification_executable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Run an unsafe verification executable.",
                    workspace_path=temp_dir,
                    context={"verification_command": ["rm", "-rf", "unsafe"]},
                )

                self.assertEqual(result["summary"]["verification"]["status"], "failed")
                self.assertEqual(result["summary"]["verification"]["details"], "verification_command_not_allowed")
            finally:
                service.shutdown()

    def test_run_task_rejects_verification_commands_not_allowed_by_capability_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Run a verification command that the capability contract denies.",
                    workspace_path=temp_dir,
                    context={
                        "verification_command": ["pytest", "-q"],
                        "capability_contract": {"allowed_commands": ["python"]},
                    },
                )

                self.assertEqual(result["summary"]["verification"]["status"], "failed")
                self.assertEqual(result["summary"]["verification"]["details"], "verification_command_not_allowed")
            finally:
                service.shutdown()

    def test_run_task_accepts_string_explicit_verification_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "verify.py").write_text("print('explicit verification')\n", encoding="utf-8")
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Run an explicit string verification command.",
                    workspace_path=temp_dir,
                    context={"verification_command": "python verify.py"},
                )

                self.assertEqual(result["summary"]["verification"]["status"], "passed")
                self.assertEqual(result["summary"]["verification"]["command"], ["python", "verify.py"])
            finally:
                service.shutdown()

    def test_run_task_rejects_path_escaping_pytest_verification_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            workspace.mkdir()
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Run an unsafe pytest target.",
                    workspace_path=str(workspace),
                    context={"verification_command": ["pytest", "../outside_tests"]},
                )

                self.assertEqual(result["summary"]["verification"]["status"], "failed")
                self.assertEqual(result["summary"]["verification"]["details"], "verification_command_outside_workspace")
            finally:
                service.shutdown()

    def test_run_task_rejects_discovered_verification_command_not_allowed_by_capability_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "tests").mkdir()
            (workspace / "tests" / "test_sample.py").write_text("def test_sample():\n    assert True\n", encoding="utf-8")
            (workspace / "Makefile").write_text("test:\n\tpython -m pytest -q tests/test_sample.py\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Run the repository's discovered verification command.",
                    workspace_path=temp_dir,
                    context={"capability_contract": {"allowed_commands": ["python"]}},
                )

                self.assertEqual(result["summary"]["verification"]["status"], "failed")
                self.assertEqual(result["summary"]["verification"]["details"], "verification_command_not_allowed")
                self.assertEqual(result["summary"]["verification"]["command"], ["make", "test"])
            finally:
                service.shutdown()

    def test_run_task_emits_progress_artifacts_for_each_iteration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")
            (workspace / "tests").mkdir()
            (workspace / "tests" / "test_sample.py").write_text("def test_sample():\n    assert True\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Prepare the repository for a small maintenance task.",
                    workspace_path=temp_dir,
                    context={"max_iterations": 2},
                )

                self.assertEqual(result["status"], "COMPLETED")
                self.assertIn("progress_artifacts", result["summary"])
                self.assertGreaterEqual(len(result["summary"]["progress_artifacts"]), 1)
            finally:
                service.shutdown()

    def test_run_task_attaches_relevant_memories_to_the_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                service.remember("lesson", "Prefer explicit verification markers for new repos.", topic=str(workspace), source="test")
                result = service.run_task(
                    "Prepare the repository for a small maintenance task.",
                    workspace_path=temp_dir,
                )

                self.assertEqual(result["status"], "COMPLETED")
                self.assertIn("relevant_memories", result["summary"])
                self.assertTrue(result["summary"]["relevant_memories"])
            finally:
                service.shutdown()

    def test_run_task_ranks_memories_by_keyword_relevance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                service.remember("lesson", "Use pytest to verify repository changes before declaring success.", topic=str(workspace), source="test")
                service.remember("lesson", "Ignore weather reports while working on code.", topic=str(workspace), source="test")
                result = service.run_task(
                    "Verify the repository changes with pytest before finishing.",
                    workspace_path=temp_dir,
                )

                self.assertEqual(result["status"], "COMPLETED")
                self.assertTrue(result["summary"]["relevant_memories"])
                self.assertEqual(
                    result["summary"]["relevant_memories"][0]["content"],
                    "Use pytest to verify repository changes before declaring success.",
                )
            finally:
                service.shutdown()

    def test_run_task_records_memory_feedback_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                service.remember(
                    "lesson",
                    "Use pytest to verify repository changes before declaring success.",
                    topic=str(workspace),
                    source="test",
                    metadata={"confidence": 0.9, "provenance": "manual"},
                )
                result = service.run_task(
                    "Verify the repository changes with pytest before finishing.",
                    workspace_path=temp_dir,
                )

                self.assertEqual(result["status"], "COMPLETED")
                relevant = result["summary"]["relevant_memories"]
                self.assertTrue(relevant)
                self.assertTrue(any(memory["metadata"].get("last_used_at") for memory in relevant))
                self.assertTrue(any(memory["metadata"].get("outcome") in {"helpful", "neutral", "harmful"} for memory in relevant))
            finally:
                service.shutdown()

    def test_run_task_ranks_helpful_memories_above_similar_less_useful_memories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                service.remember(
                    "lesson",
                    "Use pytest to verify repository changes before declaring success.",
                    topic=str(workspace),
                    source="test",
                    metadata={"confidence": 0.8, "provenance": "manual", "outcome": "helpful", "usage_count": 3},
                )
                service.remember(
                    "lesson",
                    "Use pytest to verify repository changes before declaring success.",
                    topic=str(workspace),
                    source="test",
                    metadata={"confidence": 0.8, "provenance": "manual", "outcome": "harmful", "usage_count": 1},
                )
                result = service.run_task(
                    "Verify the repository changes with pytest before finishing.",
                    workspace_path=temp_dir,
                )

                self.assertEqual(result["status"], "COMPLETED")
                relevant = result["summary"]["relevant_memories"]
                self.assertTrue(relevant)
                self.assertEqual(relevant[0]["metadata"].get("outcome"), "helpful")
            finally:
                service.shutdown()

    def test_runtime_service_redacts_sensitive_values_before_storing_memories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                payload = service.remember("lesson", "Use token sk-test-1234 and api_key=abc123", topic="repo", source="test")
                self.assertIn("[REDACTED]", payload["content"])
                self.assertNotIn("sk-test-1234", payload["content"])
                self.assertNotIn("abc123", payload["content"])
            finally:
                service.shutdown()

    def test_memory_store_filters_expired_entries_and_keeps_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMemoryStore(Path(temp_dir) / "memories.db", max_entries=10)
            try:
                store.add("lesson", "fresh lesson", "repo", "test", metadata={"confidence": 0.9, "provenance": "manual"})
                store.add("lesson", "stale lesson", "repo", "test", metadata={"confidence": 0.2, "provenance": "manual", "expires_at": "2000-01-01T00:00:00Z"})

                memories = store.list(topic="repo", limit=10)
                self.assertEqual(len(memories), 1)
                self.assertEqual(memories[0]["content"], "fresh lesson")
                self.assertEqual(memories[0]["metadata"]["confidence"], 0.9)
                self.assertEqual(memories[0]["metadata"]["provenance"], "manual")
            finally:
                store.close()

    def test_run_task_persists_a_structured_repository_task_spec(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Verify the repository changes with pytest before finishing.",
                    workspace_path=temp_dir,
                    context={
                        "verification_command": "pytest -q",
                        "expected_files": ["README.md"],
                        "allowed_tools": ["workspace_inspection", "repo_editing"],
                        "require_policy_context": True,
                    },
                )

                self.assertEqual(result["status"], "COMPLETED")
                self.assertIn("task_spec", result["summary"])
                self.assertEqual(result["summary"]["task_spec"]["description"], "Verify the repository changes with pytest before finishing.")
                self.assertEqual(result["summary"]["task_spec"]["verification_command"], "pytest -q")
                self.assertIn("README.md", result["summary"]["task_spec"]["expected_files"])
                self.assertEqual(result["summary"]["task_spec"]["selected_files"], ["README.md"])
                self.assertEqual(result["summary"]["task_spec"]["verification_expectations"]["command"], "pytest -q")
                self.assertTrue(result["summary"]["task_spec"]["verification_expectations"]["required"])
                self.assertEqual(result["summary"]["task_spec"]["patch_preview"]["status"], "not_ready")
            finally:
                service.shutdown()

    def test_run_task_updates_patch_preview_when_a_patch_is_proposed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Update the README with a small note.",
                    workspace_path=temp_dir,
                    context={
                        "dry_run": True,
                        "max_iterations": 1,
                        "proposed_patch": {"path": "README.md", "content": "maintenance task\nUpdated\n"},
                    },
                )

                self.assertIn(result["status"], {"COMPLETED", "IN_PROGRESS"})
                patch_preview = result["summary"]["task_spec"]["patch_preview"]
                self.assertEqual(patch_preview["status"], "ready")
                self.assertEqual(patch_preview["path"], "README.md")
                self.assertEqual(patch_preview["content"], "maintenance task\nUpdated\n")
            finally:
                service.shutdown()

    def test_run_task_persists_plan_artifact_and_workspace_identity_in_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Review the README and summarize the next maintenance step.",
                    workspace_path=temp_dir,
                    context={
                        "dry_run": True,
                        "max_iterations": 1,
                        "expected_files": ["README.md"],
                        "require_policy_context": True,
                        "policy_context": {"approved": True, "policy_ids": ["maintenance"]},
                    },
                )

                self.assertIn(result["status"], {"COMPLETED", "IN_PROGRESS"})
                self.assertEqual(result["summary"]["task_spec"]["plan_artifact"]["preferred_target"], "README.md")
                self.assertIn("plan", result["summary"]["task_spec"]["plan_artifact"])

                checkpoint = service.get_checkpoint(result["checkpoint_id"])
                self.assertEqual(checkpoint["summary"]["task_spec"]["plan_artifact"]["preferred_target"], "README.md")
                self.assertIn("workspace_identity", checkpoint)
                self.assertEqual(checkpoint["workspace_identity"]["workspace_path"], temp_dir)
                self.assertTrue(checkpoint["workspace_identity"]["identity_hash"])
            finally:
                service.shutdown()

    def test_run_task_persists_the_execution_stop_reason_in_the_task_spec(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Review the README and summarize the next maintenance step.",
                    workspace_path=temp_dir,
                    context={
                        "dry_run": True,
                        "max_iterations": 1,
                        "expected_files": ["README.md"],
                    },
                )

                self.assertIn(result["status"], {"COMPLETED", "IN_PROGRESS"})
                self.assertEqual(result["summary"]["task_spec"]["stop_reason"], result["summary"]["execution_loop"]["stop_reason"])
                checkpoint = service.get_checkpoint(result["checkpoint_id"])
                self.assertEqual(checkpoint["summary"]["task_spec"]["stop_reason"], result["summary"]["execution_loop"]["stop_reason"])
            finally:
                service.shutdown()

    def test_run_task_completes_a_small_repository_task_with_structured_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")
            (workspace / "verify.py").write_text("print('ok')\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Update the README with a short maintenance note.",
                    workspace_path=temp_dir,
                    context={
                        "dry_run": True,
                        "max_iterations": 1,
                        "expected_files": ["README.md", "verify.py"],
                        "verification_command": ["python", "verify.py"],
                        "proposed_patch": {"path": "README.md", "content": "maintenance task\nUpdated by Hermes\n"},
                        "require_policy_context": True,
                        "policy_context": {"approved": True, "policy_ids": ["maintenance"]},
                    },
                )

                self.assertEqual(result["status"], "COMPLETED")
                self.assertIn("task_spec", result["summary"])
                self.assertEqual(result["summary"]["task_spec"]["verification_command"], ["python", "verify.py"])
                self.assertEqual(result["summary"]["task_spec"]["patch_preview"]["status"], "ready")
                self.assertEqual(result["summary"]["task_execution"]["sandbox_context"]["profile"], "standard")
                self.assertIn("review_plan", result["summary"])
                self.assertIn("verification_report", result["summary"])
            finally:
                service.shutdown()

    def test_run_task_defaults_repository_tasks_to_network_disabled_capability_contracts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Inspect the repository and summarize the next maintenance step.",
                    workspace_path=temp_dir,
                    context={"dry_run": True, "max_iterations": 1},
                )

                self.assertIn(result["status"], {"COMPLETED", "IN_PROGRESS"})
                self.assertFalse(result["summary"]["task_spec"]["capability_contract"]["allow_network"])
                self.assertFalse(result["summary"]["sandbox_context"]["capability_contract"]["allow_network"])
            finally:
                service.shutdown()

    def test_run_task_persists_a_strict_sandbox_capability_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Review the README and keep the workspace read-only.",
                    workspace_path=temp_dir,
                    context={
                        "sandbox_profile": "strict",
                        "capability_contract": {
                            "allowed_commands": ["python"],
                            "allowed_roots": [temp_dir],
                            "allowed_env_keys": ["HOME"],
                            "allow_network": False,
                            "writable_paths": [],
                        },
                    },
                )

                self.assertEqual(result["status"], "COMPLETED")
                self.assertIn("capability_contract", result["summary"]["task_spec"])
                self.assertFalse(result["summary"]["task_spec"]["capability_contract"]["allow_network"])
                self.assertEqual(result["summary"]["task_spec"]["capability_contract"]["allowed_roots"], [temp_dir])
            finally:
                service.shutdown()

    def test_run_task_surfaces_effective_sandbox_context_in_the_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Review the README and keep the workspace read-only.",
                    workspace_path=temp_dir,
                    context={
                        "sandbox_profile": "strict",
                        "capability_contract": {
                            "allowed_commands": ["python"],
                            "allowed_roots": [temp_dir],
                            "allowed_env_keys": ["HOME"],
                            "allow_network": False,
                            "writable_paths": [],
                        },
                    },
                )

                self.assertEqual(result["status"], "COMPLETED")
                self.assertIn("sandbox_context", result["summary"])
                self.assertEqual(result["summary"]["sandbox_context"]["profile"], "strict")
                self.assertTrue(result["summary"]["sandbox_context"]["enforced"])
                self.assertFalse(result["summary"]["sandbox_context"]["capability_contract"]["allow_network"])
                self.assertEqual(result["summary"]["sandbox_context"]["capability_contract"]["allowed_roots"], [temp_dir])
            finally:
                service.shutdown()

    def test_run_task_rejects_patch_application_when_capability_contract_forbids_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            allowed_root = workspace / "allowed"
            allowed_root.mkdir()
            outside_root = workspace / "outside"
            outside_root.mkdir()
            target_path = outside_root / "README.md"
            target_path.write_text("before\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Update the README in the outside directory.",
                    workspace_path=str(workspace),
                    context={
                        "dry_run": False,
                        "capability_contract": {
                            "allowed_roots": [str(workspace)],
                            "writable_paths": [str(allowed_root)],
                        },
                        "proposed_patch": {"path": "outside/README.md", "content": "after\n"},
                    },
                )

                self.assertEqual(result["status"], "COMPLETED")
                self.assertEqual(result["summary"]["patch_result"]["reason"], "capability_contract.path_not_writable")
                self.assertEqual(target_path.read_text(encoding="utf-8"), "before\n")
            finally:
                service.shutdown()

    def test_run_task_persists_a_structured_execution_envelope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Inspect the repository and summarize the next maintenance step.",
                    workspace_path=temp_dir,
                    context={
                        "dry_run": True,
                        "max_iterations": 1,
                        "require_policy_context": True,
                        "policy_context": {"approved": True, "policy_ids": ["maintenance"]},
                    },
                )

                self.assertIn(result["status"], {"COMPLETED", "IN_PROGRESS"})
                self.assertIn("task_execution", result["summary"])
                envelope = result["summary"]["task_execution"]
                self.assertEqual(envelope["schema_version"], 1)
                self.assertEqual(envelope["task"], "Inspect the repository and summarize the next maintenance step.")
                self.assertEqual(envelope["task_status"], result["task_status"])
                self.assertEqual(envelope["workspace_path"], temp_dir)
                self.assertIn("phase", envelope)
                self.assertIn("repository_plan", envelope)
            finally:
                service.shutdown()

    def test_run_task_exposes_sandbox_context_in_the_execution_envelope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Inspect the repository and summarize the next maintenance step.",
                    workspace_path=temp_dir,
                    context={
                        "dry_run": True,
                        "max_iterations": 1,
                        "sandbox_profile": "strict",
                        "capability_contract": {
                            "allowed_commands": ["python"],
                            "allowed_roots": [temp_dir],
                            "allow_network": False,
                            "writable_paths": [],
                        },
                    },
                )

                self.assertIn("task_execution", result["summary"])
                envelope = result["summary"]["task_execution"]
                self.assertEqual(envelope["sandbox_context"]["profile"], "strict")
                self.assertFalse(envelope["sandbox_context"]["capability_contract"]["allow_network"])
                self.assertEqual(envelope["sandbox_context"]["capability_contract"]["allowed_commands"], ["python"])
            finally:
                service.shutdown()

    def test_run_task_exposes_verification_expectations_and_transitions_in_the_execution_envelope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Inspect the repository and summarize the next maintenance step.",
                    workspace_path=temp_dir,
                    context={
                        "dry_run": True,
                        "max_iterations": 1,
                        "verification_command": "pytest -q",
                        "expected_files": ["README.md"],
                    },
                )

                self.assertIn("task_execution", result["summary"])
                envelope = result["summary"]["task_execution"]
                self.assertEqual(envelope["verification_expectations"]["command"], "pytest -q")
                self.assertEqual(envelope["verification_expectations"]["required"], True)
                self.assertEqual(envelope["task_transitions"], result["summary"]["task_transitions"])
            finally:
                service.shutdown()

    def test_run_task_reports_explicit_execution_phase_in_the_summary_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Inspect the repository and prepare a patch preview.",
                    workspace_path=temp_dir,
                    context={
                        "dry_run": True,
                        "max_iterations": 1,
                        "expected_files": ["README.md"],
                    },
                )

                self.assertEqual(result["summary"]["execution_loop"]["phase"], "plan")
                self.assertEqual(result["summary"]["task_execution"]["phase"], "plan")
                checkpoint = service.get_checkpoint(result["checkpoint_id"])
                self.assertEqual(checkpoint["progress_summary"]["phase"], "plan")
            finally:
                service.shutdown()

    def test_memory_store_prunes_old_entries_when_limit_is_reached(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SQLiteMemoryStore(Path(temp_dir) / "memories.db", max_entries=2)
            try:
                store.add("lesson", "first", "repo", "test")
                store.add("lesson", "second", "repo", "test")
                store.add("lesson", "third", "repo", "test")

                memories = store.list(topic="repo", limit=10)
                self.assertEqual(len(memories), 2)
                self.assertEqual([memory["content"] for memory in memories], ["second", "third"])
            finally:
                store.close()

    def test_run_task_persists_a_compact_progress_summary_in_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Inspect the repository and summarize the next maintenance step.",
                    workspace_path=temp_dir,
                    context={"max_iterations": 1, "dry_run": True},
                )

                checkpoint = service.get_checkpoint(result["checkpoint_id"])
                self.assertIn("progress_summary", checkpoint)
                self.assertEqual(checkpoint["progress_summary"]["phase"], result["summary"]["execution_loop"]["phase"])
                self.assertEqual(checkpoint["progress_summary"]["stop_reason"], result["summary"]["execution_loop"]["stop_reason"])
            finally:
                service.shutdown()

    def test_run_task_surfaces_provider_routing_choice_in_the_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Prepare the repository for a small maintenance task.",
                    workspace_path=temp_dir,
                    context={"provider": "stub"},
                )

                self.assertEqual(result["status"], "COMPLETED")
                self.assertIn("provider_routing", result["summary"])
                self.assertEqual(result["summary"]["provider_routing"]["provider"], "stub")
                self.assertIn("reason", result["summary"]["provider_routing"])
                self.assertIn("health_summary", result["summary"]["provider_routing"])
                self.assertEqual(result["summary"]["provider_routing"]["health_summary"]["selected_provider"], "stub")
            finally:
                service.shutdown()

    def test_run_task_persists_provider_routing_data_in_the_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Prepare the repository for a small maintenance task.",
                    workspace_path=temp_dir,
                    context={"provider": "stub"},
                )

                checkpoint = service.get_checkpoint(result["checkpoint_id"])
                self.assertIn("provider_routing", checkpoint)
                self.assertEqual(checkpoint["provider_routing"]["provider"], "stub")
                self.assertEqual(checkpoint["provider_routing"]["health_summary"]["selected_provider"], "stub")
            finally:
                service.shutdown()

    def test_run_task_exposes_structured_memory_context_in_the_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                service.remember(
                    "lesson",
                    "Prefer explicit verification markers for new repos.",
                    topic=str(workspace),
                    source="test",
                    metadata={"task": "validate", "confidence": 0.8, "provenance": "manual"},
                )
                result = service.run_task(
                    "Verify the repository changes with pytest before finishing.",
                    workspace_path=temp_dir,
                )

                self.assertEqual(result["status"], "COMPLETED")
                self.assertIn("memory_context", result["summary"])
                self.assertEqual(result["summary"]["memory_context"]["retrieval_topic"], str(workspace))
                self.assertGreaterEqual(result["summary"]["memory_context"]["retrieved_count"], 1)
                self.assertTrue(result["summary"]["memory_context"]["memories"])
                self.assertTrue(result["summary"]["memory_context"]["memories"][0]["metadata"].get("advisory", False))
                self.assertEqual(result["summary"]["memory_context"]["memories"][0]["metadata"]["provenance"], "manual")
            finally:
                service.shutdown()

    def test_run_task_uses_relevant_memory_to_choose_a_next_action(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                service.remember(
                    "lesson",
                    "Use pytest to verify repository changes before retrying a failed task.",
                    topic=str(workspace),
                    source="test",
                )
                result = service.run_task(
                    "Verify the repository changes after a failed task.",
                    workspace_path=temp_dir,
                )

                influence = result["summary"]["memory_context"]["influence"]
                self.assertEqual(influence["strategy"], "repository_verification")
                self.assertIn("pytest", influence["next_action"].lower())
                self.assertTrue(any("memory" in step.lower() for step in result["summary"]["next_steps"]))
            finally:
                service.shutdown()

    def test_run_task_produces_repo_aware_next_steps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "README.md").write_text("maintenance task\n", encoding="utf-8")
            (workspace / "tests").mkdir()
            (workspace / "tests" / "test_sample.py").write_text("def test_sample():\n    assert True\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Prepare the repository for a small maintenance task.",
                    workspace_path=temp_dir,
                )

                self.assertEqual(result["status"], "COMPLETED")
                self.assertIn("next_steps", result["summary"])
                self.assertTrue(result["summary"]["next_steps"])
                self.assertTrue(any("tests" in step.lower() or "pytest" in step.lower() for step in result["summary"]["next_steps"]))
            finally:
                service.shutdown()

    def test_runtime_service_can_list_and_inspect_checkpoints(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                result = service.run_task(
                    "Inspect the repository and summarize the next maintenance step.",
                    workspace_path=temp_dir,
                    context={"max_iterations": 1},
                )

                self.assertIn("checkpoint_id", result)
                checkpoints = service.list_checkpoints()
                self.assertTrue(checkpoints)
                checkpoint = service.get_checkpoint(result["checkpoint_id"])
                self.assertEqual(checkpoint["checkpoint_id"], result["checkpoint_id"])
                self.assertIn("summary", checkpoint)
            finally:
                service.shutdown()

    def test_start_run_returns_an_initial_run_record_for_background_execution(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                started = service.start_run(str(workflow_path))

                self.assertNotEqual(started["status"], "FAILED")
                self.assertIn("run_id", started)
                self.assertIn("execution_id", started)

                observed = service.observe_run(started["run_id"])
                self.assertEqual(observed["run_id"], started["run_id"])
            finally:
                service.shutdown()

    def test_start_run_persists_normalized_policy_context(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                started = service.start_run(
                    str(workflow_path),
                    context={"policy_context": {"approved": True, "policy_ids": ["operator-approved"]}},
                )

                run_record = service.get_run(started["run_id"])
                execution = service.kernel.workflow_executions[started["execution_id"]]
                self.assertEqual(run_record["context"]["policy_context"]["policy_ids"], ["operator-approved"])
                self.assertEqual(execution.policy_context["policy_ids"], ["operator-approved"])
            finally:
                service.shutdown()

    def test_bridge_supports_start_status_artifact_and_approval_flow(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "approval_example.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                started = service.start_run(str(workflow_path))

                self.assertEqual(started["status"], "WAITING_APPROVAL")
                self.assertIn("run_id", started)
                self.assertIn("execution_id", started)

                status = service.get_run(started["run_id"])
                self.assertEqual(status["status"], "WAITING_APPROVAL")
                self.assertEqual(status["workflow_name"], "approval_example")
                self.assertIn("last_event_id", status)

                observed = service.observe_run(started["run_id"])
                self.assertTrue(observed["events"])

                artifacts = service.get_run_artifacts(started["run_id"])
                self.assertTrue(artifacts)

                approved = service.respond_approval(started["run_id"], "approve")
                self.assertTrue(approved["accepted"])

                completed = service.get_run(started["run_id"])
                self.assertEqual(completed["status"], "COMPLETED")
            finally:
                service.shutdown()

    def test_cancel_run_marks_run_as_cancelled(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "approval_example.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                started = service.start_run(str(workflow_path))
                cancelled = service.cancel_run(started["run_id"])

                self.assertTrue(cancelled["accepted"])
                self.assertEqual(cancelled["status"], "CANCELLED")
                self.assertEqual(service.get_run(started["run_id"])["status"], "CANCELLED")
            finally:
                service.shutdown()

    def test_run_observation_exposes_summary_and_pending_approval_details(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "approval_example.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                started = service.start_run(str(workflow_path))

                run_state = service.get_run(started["run_id"])
                self.assertEqual(run_state["summary"]["status"], "WAITING_APPROVAL")
                self.assertEqual(run_state["summary"]["pending_approval_role"], "human_operator")
                self.assertGreaterEqual(run_state["summary"]["artifact_count"], 1)

                observed = service.observe_run(started["run_id"])
                self.assertIn("summary", observed)
                self.assertEqual(observed["summary"]["pending_approval_role"], "human_operator")
            finally:
                service.shutdown()

    def test_start_run_accepts_execution_context(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                started = service.start_run(str(workflow_path), context={"execution_mode": "manual"})
                workflow_execution = service.kernel.workflow_executions[started["execution_id"]]
                self.assertEqual(workflow_execution.execution_context.execution_mode, "manual")
            finally:
                service.shutdown()

    def test_append_event_and_finalize_run_update_run_state(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "approval_example.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                started = service.start_run(str(workflow_path))

                appended = service.append_event(started["run_id"], {"event_id": "ui-event-1", "event_type": "MESSAGE", "payload": {"text": "hello"}})
                self.assertTrue(appended["accepted"])
                self.assertEqual(service.get_run(started["run_id"])["last_event_id"], "ui-event-1")

                finalized = service.finalize_run(started["run_id"], "COMPLETED")
                self.assertTrue(finalized["accepted"])
                self.assertEqual(service.get_run(started["run_id"])["status"], "COMPLETED")
            finally:
                service.shutdown()

    def test_start_run_persists_context_and_dispatches_rich_control_events(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                started = service.start_run(
                    str(workflow_path),
                    context={
                        "agent_context": {"session_id": "session-1", "goal": "draft a plan"},
                        "run_metadata": {"requested_by": "user"},
                    },
                )

                run_state = service.get_run(started["run_id"])
                self.assertEqual(run_state["context"]["agent_context"]["session_id"], "session-1")
                self.assertEqual(run_state["context"]["run_metadata"]["requested_by"], "user")

                dispatched = service.handle_action(
                    started["run_id"],
                    "queue_message",
                    {"text": "still working"},
                    actor="agent",
                    correlation_id="corr-1",
                )
                self.assertTrue(dispatched["accepted"])

                observed = service.observe_run(started["run_id"])
                self.assertTrue(any(event.get("event_type") == "MESSAGE_ENQUEUED" for event in observed["events"]))
            finally:
                service.shutdown()

    def test_queue_run_creates_a_persisted_run_and_can_resume_after_restart(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                queued = service.queue_run(str(workflow_path))
                self.assertEqual(queued["status"], "QUEUED")
                self.assertIn("run_id", queued)

                time.sleep(0.2)
                resumed = service.get_run(queued["run_id"])
                self.assertNotEqual(resumed["status"], "QUEUED")
            finally:
                service.shutdown()

    def test_background_run_restart_increments_recovery_restart_count(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "approval_example.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                queued = service.queue_run(str(workflow_path))
                run_id = queued["run_id"]
                self.assertEqual(queued["status"], "QUEUED")
            finally:
                service.shutdown()

            service = RuntimeService(data_dir=temp_dir)
            try:
                time.sleep(0.2)
                resumed = service.get_run(run_id)
                self.assertTrue(resumed["recovery"]["restart_count"] >= 1)
                self.assertIn(resumed["status"], {"WAITING_APPROVAL", "RUNNING", "COMPLETED"})
            finally:
                service.shutdown()

    def test_queue_run_persists_provider_for_resume(self):
        """P1-2: queue_run must persist provider/model/endpoint on the run
        record so ``_resume_pending_runs`` can recover the exact routing after
        a service restart (the ``RUN_PROVIDER_ROUTED`` event is an audit trail
        and is asserted separately to avoid racing the background thread)."""
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                service.queue_run(
                    str(workflow_path),
                    provider="ollama",
                    model_name="qwen3.8:27b",
                    endpoint="http://localhost:11434/v1/chat/completions",
                )
                run_id = service.run_store.list()[0]["run_id"]
                record = service.run_store.get(run_id)
                self.assertEqual(record["resume_provider"], "ollama")
                self.assertEqual(record["resume_model_name"], "qwen3.8:27b")
                self.assertEqual(record["resume_endpoint"], "http://localhost:11434/v1/chat/completions")
                self.assertEqual(record["selected_provider"], "ollama")
            finally:
                service.shutdown()

    def test_resume_run_recovers_persisted_provider(self):
        """P1-2 (recovery half): ``_resume_pending_runs`` must re-launch the
        workflow runner with the exact provider/model/endpoint that were
        persisted at enqueue time -- not default to the 'stub' provider.

        Seeds a run record in the exact shape ``queue_run`` produces (see
        the persistence block in ``runtime_service.queue_run``), then calls
        ``_resume_pending_runs`` directly with a patched
        ``_run_queued_workflow`` so the captured ``.kwargs`` are deterministic.
        """
        execution_id = "execution-test-seed-0001"
        run_id = "run-test-seed-0001"
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                # Seed the persisted run record -- the same contract that
                # ``queue_run`` writes to disk.
                service.run_store.create(run_id, {
                    "run_id": run_id,
                    "execution_id": execution_id,
                    "workflow_name": "seeded",
                    "workflow_path": str(workflow_path),
                    "status": "QUEUED",
                    "queued_for_background": True,
                    "recovery": {},
                    "provider_routing": {"selected_provider": "ollama"},
                    "selected_provider": "ollama",
                    "resume_provider": "ollama",
                    "resume_model_name": "qwen3.8:27b",
                    "resume_endpoint": "http://localhost:11434/v1/chat/completions",
                    "idempotency_key": None,
                })
                # Kernel status: RUNNING is non-terminal so _resume_pending_runs
                # proceeds to re-launch the background thread.
                service.kernel.get_workflow_status = lambda eid: "RUNNING"

                # Patch threading.Thread so the test is fully synchronous -- no
                # real thread starts, and we capture the Thread constructor's
                # kwargs (target / args / kwargs) directly.
                with mock.patch("threading.Thread") as fake_thread_cls:
                    service._resume_pending_runs()

                # The Thread constructor must have been called exactly for our
                # seeded run; inspect its captured kwargs.
                self.assertTrue(
                    fake_thread_cls.call_args_list,
                    "expected _resume_pending_runs to construct a background Thread",
                )
                thread_ctor = fake_thread_cls.call_args_list[-1].kwargs
                self.assertIn("kwargs", thread_ctor, "Thread must be constructed with kwargs dict")
                captured = thread_ctor["kwargs"]
                self.assertEqual(captured.get("provider"), "ollama")
                self.assertEqual(captured.get("model_name"), "qwen3.8:27b")
                self.assertEqual(captured.get("endpoint"), "http://localhost:11434/v1/chat/completions")

                # Sanity: the record still carries the resume_* fields.
                record = service.run_store.get(run_id)
                self.assertEqual(record.get("resume_provider"), "ollama")
                self.assertEqual(record.get("resume_model_name"), "qwen3.8:27b")
                self.assertEqual(record.get("resume_endpoint"), "http://localhost:11434/v1/chat/completions")
            finally:
                service.shutdown()

    def test_respond_approval_deny_routes_to_cancel(self):
        """P1-3: a deny-family choice on a workflow-gated run (no pending tool
        approval) must land in a terminal negative (CANCELLED), not silently
        no-op. This is exactly what the WebUI 'Reject' button drives."""
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "approval_example.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                started = service.start_run(str(workflow_path))
                run_id = started["run_id"]
                self.assertEqual(service.get_run(run_id)["status"], "WAITING_APPROVAL")

                response = service.respond_approval(run_id, choice="deny")
                self.assertFalse(response.get("approved", False))
                self.assertEqual(response.get("status"), "CANCELLED")
                self.assertEqual(service.get_run(run_id)["status"], "CANCELLED")
            finally:
                service.shutdown()

    def test_respond_approval_deny_with_stale_approval_id_surfaces_error(self):
        """P1-3: a deny with a stale/unknown ``approval_id`` must surface the
        deny error (NOT_FOUND) rather than silently swallow the intent."""
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "approval_example.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                started = service.start_run(str(workflow_path))
                run_id = started["run_id"]
                response = service.respond_approval(run_id, approval_id="tool-approval-deadbeef", choice="deny")
                self.assertIn(response.get("status"), {"NOT_FOUND", "REJECTED", "DENIED", "CANCELLED"})
                self.assertFalse(response.get("approved", False))
            finally:
                service.shutdown()

    def test_observe_run_exposes_richer_event_metadata(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                started = service.start_run(str(workflow_path))
                service.handle_action(started["run_id"], "queue_message", {"text": "still working"}, actor="agent", correlation_id="corr-2")
                observed = service.observe_run(started["run_id"])
                self.assertTrue(observed["events"])
                self.assertTrue(any(event.get("source") == "bridge" for event in observed["events"]))
                self.assertTrue(any(event.get("status_after") for event in observed["events"]))
            finally:
                service.shutdown()

    def test_run_payload_exposes_checkpoint_and_recovery_hints(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                started = service.start_run(str(workflow_path))
                run_state = service.get_run(started["run_id"])
                self.assertIn("checkpoint", run_state)
                self.assertIn("recovery", run_state)
                self.assertIn("resume_hint", run_state)
                self.assertIn("latest_checkpoint_id", run_state)
                self.assertIn(run_state["resume_hint"]["reason"], {"checkpoint available", "no checkpoint persisted yet"})
            finally:
                service.shutdown()

    def test_resume_control_action_rejects_non_resumable_runs(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                started = service.start_run(str(workflow_path))
                run_record = service.run_store.get(started["run_id"])
                run_record["status"] = "RUNNING"
                service.run_store.update(started["run_id"], run_record)
                resumed = service.handle_action(started["run_id"], "resume_run")
                self.assertFalse(resumed["accepted"])
                self.assertEqual(resumed["status"], "COMPLETED")
            finally:
                service.shutdown()

    def test_resume_control_action_rejects_terminal_runs(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                started = service.start_run(str(workflow_path))
                service.finalize_run(started["run_id"], "COMPLETED")
                finalized = service.handle_action(started["run_id"], "resume_run")
                self.assertFalse(finalized["accepted"])
                self.assertEqual(finalized["status"], "COMPLETED")
            finally:
                service.shutdown()

    def test_start_run_persists_agent_context_and_recovery_liveness(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "approval_example.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                started = service.start_run(
                    str(workflow_path),
                    context={
                        "session_id": "sess-7",
                        "agent_goal": "prepare review",
                        "workflow_template": "approval-template",
                        "runtime_hints": {"timeout_policy": "short"},
                    },
                )
                run_state = service.get_run(started["run_id"])
                self.assertEqual(run_state["context"]["session_id"], "sess-7")
                self.assertEqual(run_state["context"]["agent_goal"], "prepare review")
                self.assertEqual(run_state["context"]["workflow_template"], "approval-template")
                self.assertIn("liveness", run_state)
                self.assertIn("last_heartbeat_at", run_state["liveness"])
                self.assertIn("recovery", run_state)
                self.assertEqual(run_state["recovery"]["restart_count"], 0)
            finally:
                service.shutdown()

    def test_auto_approve_context_allows_unattended_approval_flow(self):
        workflow_path = Path(__file__).resolve().parent.parent / "examples" / "approval_example.json"

        with tempfile.TemporaryDirectory() as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                started = service.start_run(str(workflow_path), context={"auto_approve": True})
                time.sleep(0.2)
                current = service.get_run(started["run_id"])
                self.assertEqual(current["status"], "COMPLETED")
            finally:
                service.shutdown()


if __name__ == "__main__":
    unittest.main()
