import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.models import ToolRequest
from engine.runtime_service import RuntimeService


class TestToolValidationRetries(unittest.TestCase):
    def test_execute_tool_request_rejects_invalid_parameters(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-validation-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                request = ToolRequest(tool_id="filesystem", action="read_file", parameters={})
                envelopes = service.execute_tool_request(request)
                self.assertEqual(len(envelopes), 1)
                self.assertEqual(envelopes[0].status, "error")
                self.assertIn("path", envelopes[0].error)
            finally:
                service.shutdown()

    def test_execute_tool_request_retries_transient_failures(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-retry-", dir="/tmp") as temp_dir:
            workspace = Path(temp_dir)
            service = RuntimeService(data_dir=temp_dir)
            try:
                request = ToolRequest(tool_id="shell", action="run", parameters={"command": "python -c 'import sys; sys.exit(0)'"})
                envelopes = service.execute_tool_request(request, max_retries=2)
                self.assertEqual(len(envelopes), 1)
                self.assertEqual(envelopes[0].status, "ok")
                self.assertEqual(envelopes[0].metadata.get("attempts"), 1)
            finally:
                service.shutdown()

    def test_execute_tool_request_emits_run_lifecycle_events(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-events-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
                started = service.start_run(str(workflow_path))
                request = ToolRequest(tool_id="filesystem", action="read_file", parameters={"path": str(Path(temp_dir) / "missing.txt")})
                envelopes = service.execute_tool_request(request, run_id=started["run_id"])
                self.assertEqual(len(envelopes), 1)
                self.assertEqual(envelopes[0].status, "ok")

                run_state = service.get_run(started["run_id"])
                event_types = [event.get("event_type") for event in run_state.get("events", [])]
                self.assertIn("TOOL_EXECUTION_STARTED", event_types)
                self.assertIn("TOOL_EXECUTION_COMPLETED", event_types)
            finally:
                service.shutdown()

    def test_execute_tool_request_records_duration_and_classification(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-observability-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
                started = service.start_run(str(workflow_path))
                marker = Path(temp_dir) / "marker.txt"
                marker.write_text("hello\n", encoding="utf-8")
                request = ToolRequest(tool_id="filesystem", action="read_file", parameters={"path": str(marker)})
                envelopes = service.execute_tool_request(request, run_id=started["run_id"])

                self.assertEqual(envelopes[0].status, "ok")
                self.assertIn("duration_ms", envelopes[0].metadata or {})
                self.assertEqual(envelopes[0].metadata.get("classification"), "success")

                run_state = service.get_run(started["run_id"])
                completed_event = next(event for event in run_state.get("events", []) if event.get("event_type") == "TOOL_EXECUTION_COMPLETED")
                self.assertIn("duration_ms", completed_event.get("payload", {}))
                self.assertEqual(completed_event.get("payload", {}).get("classification"), "success")
            finally:
                service.shutdown()

    def test_execute_tool_request_uses_contract_timeout(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-timeout-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                request = ToolRequest(tool_id="shell", action="run", parameters={"command": "python -c 'import time; time.sleep(2)'", "timeout": 0.01})
                envelopes = service.execute_tool_request(request)
                self.assertEqual(len(envelopes), 1)
                self.assertEqual(envelopes[0].status, "error")
                self.assertIn("timeout", envelopes[0].error.lower())
            finally:
                service.shutdown()

    def test_execute_tool_request_requires_approval_for_destructive_actions(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-approval-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
                started = service.start_run(str(workflow_path))
                marker = Path(temp_dir) / "delete-me.txt"
                marker.write_text("remove me\n", encoding="utf-8")

                request = ToolRequest(tool_id="filesystem", action="delete_file", parameters={"path": str(marker)})
                envelopes = service.execute_tool_request(request, run_id=started["run_id"])

                self.assertEqual(len(envelopes), 1)
                self.assertEqual(envelopes[0].status, "pending_approval")
                self.assertTrue(envelopes[0].metadata.get("approval_id"))

                pending = service.list_pending_tool_approvals(started["run_id"])
                self.assertEqual(len(pending), 1)
                approved = service.approve_tool_request(started["run_id"], pending[0]["approval_id"], approved_by="test-user")
                self.assertTrue(approved["accepted"])
                self.assertFalse(marker.exists())

                run_state = service.get_run(started["run_id"])
                event_types = [event.get("event_type") for event in run_state.get("events", [])]
                self.assertIn("TOOL_APPROVAL_GRANTED", event_types)
                self.assertEqual(service.list_pending_tool_approvals(started["run_id"]), [])
            finally:
                service.shutdown()

    def test_deny_tool_request_records_run_level_denial(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-approval-deny-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
                started = service.start_run(str(workflow_path))
                marker = Path(temp_dir) / "delete-me.txt"
                marker.write_text("remove me\n", encoding="utf-8")

                request = ToolRequest(tool_id="filesystem", action="delete_file", parameters={"path": str(marker)})
                envelopes = service.execute_tool_request(request, run_id=started["run_id"])
                self.assertEqual(envelopes[0].status, "pending_approval")

                pending = service.list_pending_tool_approvals(started["run_id"])[0]
                denied = service.deny_tool_request(started["run_id"], pending["approval_id"], denied_by="test-user")
                self.assertTrue(denied["accepted"])
                self.assertEqual(service.list_pending_tool_approvals(started["run_id"]), [])
                self.assertTrue(marker.exists())

                run_state = service.get_run(started["run_id"])
                event_types = [event.get("event_type") for event in run_state.get("events", [])]
                self.assertIn("TOOL_APPROVAL_DENIED", event_types)
            finally:
                service.shutdown()

    def test_approve_tool_request_reuses_original_step_context(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-approval-step-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
                started = service.start_run(str(workflow_path))
                marker = Path(temp_dir) / "delete-me.txt"
                marker.write_text("remove me\n", encoding="utf-8")

                request = ToolRequest(tool_id="filesystem", action="delete_file", parameters={"path": str(marker)})
                envelopes = service.execute_tool_request(request, run_id=started["run_id"])
                self.assertEqual(envelopes[0].status, "pending_approval")

                approval = service.list_pending_tool_approvals(started["run_id"])[0]
                with mock.patch.object(service, "execute_tool_request", wraps=service.execute_tool_request) as patched_execute:
                    approved = service.approve_tool_request(started["run_id"], approval["approval_id"], approved_by="test-user")

                self.assertTrue(approved["accepted"])
                self.assertEqual(patched_execute.call_count, 1)
                self.assertEqual(patched_execute.call_args.kwargs.get("step_execution_id"), approval["step_execution_id"])
            finally:
                service.shutdown()

    def test_execute_tool_request_fails_closed_for_high_risk_without_run_context(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-policy-context-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                marker = Path(temp_dir) / "delete-me.txt"
                marker.write_text("remove me\n", encoding="utf-8")
                request = ToolRequest(tool_id="filesystem", action="delete_file", parameters={"path": str(marker)})

                envelopes = service.execute_tool_request(request)

                self.assertEqual(envelopes[0].status, "error")
                self.assertEqual(envelopes[0].error, "tool_approval_context_required")
                self.assertTrue(marker.exists())
            finally:
                service.shutdown()

    def test_execute_tool_request_requires_policy_context_for_high_risk_tools(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-policy-context-run-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
                started = service.start_run(str(workflow_path), context={"require_policy_context": True})
                marker = Path(temp_dir) / "delete-me.txt"
                marker.write_text("remove me\n", encoding="utf-8")
                request = ToolRequest(tool_id="filesystem", action="delete_file", parameters={"path": str(marker)})

                envelopes = service.execute_tool_request(request, run_id=started["run_id"])

                self.assertEqual(envelopes[0].status, "error")
                self.assertEqual(envelopes[0].error, "policy_context.missing")
                self.assertTrue(marker.exists())
            finally:
                service.shutdown()

    def test_execute_tool_request_allows_high_risk_tools_with_policy_context(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-policy-context-allowed-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
                started = service.start_run(str(workflow_path), context={"require_policy_context": True, "policy_context": {"approved": True, "policy_ids": ["maintenance"]}})
                marker = Path(temp_dir) / "delete-me.txt"
                marker.write_text("remove me\n", encoding="utf-8")
                request = ToolRequest(tool_id="filesystem", action="delete_file", parameters={"path": str(marker)})

                envelopes = service.execute_tool_request(request, run_id=started["run_id"])

                self.assertEqual(envelopes[0].status, "pending_approval")
                self.assertTrue(envelopes[0].metadata.get("approval_id"))
                self.assertTrue(marker.exists())
            finally:
                service.shutdown()

    def test_execute_tool_request_enforces_run_tool_call_budget(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-budget-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
                started = service.start_run(str(workflow_path), context={"max_tool_calls": 0})
                marker = Path(temp_dir) / "marker.txt"
                marker.write_text("hello\n", encoding="utf-8")
                request = ToolRequest(tool_id="filesystem", action="read_file", parameters={"path": str(marker)})

                envelopes = service.execute_tool_request(request, run_id=started["run_id"])

                self.assertEqual(envelopes[0].status, "error")
                self.assertEqual(envelopes[0].error, "tool_call_budget_exceeded")
                run_record = service.get_run(started["run_id"])
                self.assertEqual(run_record["budget"]["usage"]["tool_calls"], 0)
            finally:
                service.shutdown()

    def test_execute_tool_request_uses_explicit_step_context_for_approval_and_replay(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-step-context-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
                started = service.start_run(str(workflow_path))
                step_execution_id = service._find_step_execution_id_for_run(started["run_id"])
                self.assertIsNotNone(step_execution_id)

                source = Path(temp_dir) / "source.txt"
                destination = Path(temp_dir) / "destination.txt"
                source.write_text("move me\n", encoding="utf-8")
                request = ToolRequest(
                    tool_id="filesystem",
                    action="move_file",
                    parameters={"source": str(source), "destination": str(destination)},
                )

                pending = service.execute_tool_request(request, run_id=started["run_id"], step_execution_id=step_execution_id)
                self.assertEqual(pending[0].status, "pending_approval")
                approval = service.list_pending_tool_approvals(started["run_id"])[0]
                approved = service.approve_tool_request(started["run_id"], approval["approval_id"], approved_by="test-user")
                self.assertTrue(approved["accepted"])
                self.assertTrue(destination.exists())

                duplicate = service.execute_tool_request(request, run_id=started["run_id"], step_execution_id=step_execution_id)
                self.assertEqual(duplicate[0].status, "ok")
                self.assertFalse(source.exists())
                self.assertTrue(destination.exists())
            finally:
                service.shutdown()

    def test_execute_tool_request_enforces_strict_sandbox_profile(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-sandbox-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
                started = service.start_run(str(workflow_path), context={"sandbox_profile": "strict"})

                request = ToolRequest(
                    tool_id="shell",
                    action="run",
                    parameters={"command": "python -c 'print(1)'", "env": {"PATH": "/tmp"}},
                )
                envelopes = service.execute_tool_request(request, run_id=started["run_id"])

                self.assertEqual(envelopes[0].status, "error")
                self.assertIn("environment variable", envelopes[0].error.lower())
            finally:
                service.shutdown()

    def test_execute_tool_request_rejects_mutating_filesystem_actions_in_strict_sandbox(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-sandbox-filesystem-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
                started = service.start_run(str(workflow_path), context={"sandbox_profile": "strict"})
                marker = Path(temp_dir) / "marker.txt"
                marker.write_text("hello\n", encoding="utf-8")

                request = ToolRequest(
                    tool_id="filesystem",
                    action="write_file",
                    parameters={"path": str(marker), "content": "updated\n"},
                )
                envelopes = service.execute_tool_request(request, run_id=started["run_id"])

                self.assertEqual(envelopes[0].status, "error")
                self.assertIn("sandbox", envelopes[0].error.lower())
            finally:
                service.shutdown()

    def test_execute_tool_request_requires_capability_requirement(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-capability-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
                started = service.start_run(str(workflow_path), context={"capability_requirements": ["workspace_inspection"]})
                marker = Path(temp_dir) / "marker.txt"
                marker.write_text("hello\n", encoding="utf-8")

                request = ToolRequest(
                    tool_id="shell",
                    action="run",
                    parameters={"command": "python -c 'print(1)'"},
                )
                envelopes = service.execute_tool_request(request, run_id=started["run_id"])

                self.assertEqual(envelopes[0].status, "error")
                self.assertIn("capability", envelopes[0].error.lower())
            finally:
                service.shutdown()

    def test_execute_tool_request_honors_capability_contract_allowed_roots(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-capability-roots-", dir="/tmp") as temp_dir:
            service_data_dir = Path(temp_dir) / "service-data"
            service_data_dir.mkdir()
            allowed_root = Path(temp_dir) / "allowed-root"
            allowed_root.mkdir()
            marker = allowed_root / "marker.txt"
            marker.write_text("hello\n", encoding="utf-8")

            service = RuntimeService(data_dir=str(service_data_dir))
            try:
                workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
                started = service.start_run(str(workflow_path), context={"capability_contract": {"allowed_roots": [str(allowed_root)]}})
                request = ToolRequest(tool_id="filesystem", action="read_file", parameters={"path": str(marker)})
                envelopes = service.execute_tool_request(request, run_id=started["run_id"])

                self.assertEqual(envelopes[0].status, "ok")
                self.assertEqual(envelopes[0].output.get("content"), "hello\n")
            finally:
                service.shutdown()

    def test_execute_tool_request_honors_capability_contract_writable_paths_for_copy(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-capability-writable-", dir="/tmp") as temp_dir:
            service_data_dir = Path(temp_dir) / "service-data"
            service_data_dir.mkdir()
            allowed_root = Path(temp_dir) / "allowed-root"
            allowed_root.mkdir()
            source = Path(temp_dir) / "source.txt"
            source.write_text("hello\n", encoding="utf-8")
            destination = allowed_root / "copied.txt"

            service = RuntimeService(data_dir=str(service_data_dir))
            try:
                workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
                started = service.start_run(str(workflow_path), context={"capability_contract": {"writable_paths": [str(allowed_root)]}})
                request = ToolRequest(
                    tool_id="filesystem",
                    action="copy_file",
                    parameters={"source": str(source), "destination": str(destination)},
                )
                envelopes = service.execute_tool_request(request, run_id=started["run_id"])

                self.assertEqual(envelopes[0].status, "ok")
                self.assertTrue(destination.exists())
                self.assertEqual(destination.read_text(encoding="utf-8"), "hello\n")
            finally:
                service.shutdown()

    def test_approved_destructive_request_is_idempotent_by_fingerprint(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-idempotency-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
                started = service.start_run(str(workflow_path))
                source = Path(temp_dir) / "source.txt"
                destination = Path(temp_dir) / "destination.txt"
                source.write_text("move me\n", encoding="utf-8")
                request = ToolRequest(
                    tool_id="filesystem",
                    action="move_file",
                    parameters={"source": str(source), "destination": str(destination)},
                )

                pending = service.execute_tool_request(request, run_id=started["run_id"])
                self.assertEqual(pending[0].status, "pending_approval")
                approval = service.list_pending_tool_approvals(started["run_id"])[0]
                approved = service.approve_tool_request(started["run_id"], approval["approval_id"], approved_by="test-user")
                self.assertTrue(approved["accepted"])
                self.assertTrue(destination.exists())

                duplicate = service.execute_tool_request(request, run_id=started["run_id"])
                self.assertEqual(duplicate[0].status, "ok")
                self.assertTrue(destination.exists())
                self.assertFalse(source.exists())
            finally:
                service.shutdown()

    def test_approved_destructive_request_replays_from_ledger_after_restart(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-ledger-replay-", dir="/tmp") as temp_dir:
            workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
            source = Path(temp_dir) / "source.txt"
            destination = Path(temp_dir) / "destination.txt"
            source.write_text("move me\n", encoding="utf-8")

            service = RuntimeService(data_dir=temp_dir)
            started = service.start_run(str(workflow_path))
            request = ToolRequest(
                tool_id="filesystem",
                action="move_file",
                parameters={"source": str(source), "destination": str(destination)},
            )
            pending = service.execute_tool_request(request, run_id=started["run_id"])
            approval = service.list_pending_tool_approvals(started["run_id"])[0]
            service.approve_tool_request(started["run_id"], approval["approval_id"], approved_by="test-user")
            service.shutdown()

            recovered_service = RuntimeService(data_dir=temp_dir)
            try:
                replayed = recovered_service.execute_tool_request(request, run_id=started["run_id"])
                self.assertEqual(replayed[0].status, "ok")
                self.assertFalse(source.exists())
                self.assertTrue(destination.exists())
                event_types = [event.get("event_type") for event in recovered_service.get_run(started["run_id"]).get("events", [])]
                self.assertIn("TOOL_EXECUTION_REPLAYED", event_types)
            finally:
                recovered_service.shutdown()

    def test_execute_tool_request_persists_risk_level_in_completed_events(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-risk-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
                started = service.start_run(str(workflow_path))
                marker = Path(temp_dir) / "marker.txt"
                marker.write_text("hello\n", encoding="utf-8")

                request = ToolRequest(tool_id="filesystem", action="read_file", parameters={"path": str(marker)})
                envelopes = service.execute_tool_request(request, run_id=started["run_id"])

                self.assertEqual(envelopes[0].status, "ok")
                self.assertEqual(envelopes[0].metadata.get("risk_level"), "read_only")

                run_state = service.get_run(started["run_id"])
                completed_event = next(event for event in run_state.get("events", []) if event.get("event_type") == "TOOL_EXECUTION_COMPLETED")
                self.assertEqual(completed_event.get("payload", {}).get("risk_level"), "read_only")
            finally:
                service.shutdown()

    def test_execute_tool_request_persists_policy_decision_and_context(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-policy-events-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
                started = service.start_run(str(workflow_path))
                marker = Path(temp_dir) / "marker.txt"
                marker.write_text("hello\n", encoding="utf-8")
                request = ToolRequest(tool_id="filesystem", action="read_file", parameters={"path": str(marker)})

                envelopes = service.execute_tool_request(request, run_id=started["run_id"])

                self.assertEqual(envelopes[0].status, "ok")
                completed_event = next(
                    event for event in service.get_run(started["run_id"]).get("events", [])
                    if event.get("event_type") == "TOOL_EXECUTION_COMPLETED"
                )
                self.assertEqual(completed_event["payload"]["policy_decision"]["allowed"], True)
                self.assertTrue(completed_event["payload"]["policy_context"]["approved"])
            finally:
                service.shutdown()

    def test_execute_tool_request_emits_risk_level_in_lifecycle_events(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-events-risk-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                workflow_path = Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"
                started = service.start_run(str(workflow_path))
                marker = Path(temp_dir) / "marker.txt"
                marker.write_text("hello\n", encoding="utf-8")

                request = ToolRequest(tool_id="filesystem", action="read_file", parameters={"path": str(marker)})
                service.execute_tool_request(request, run_id=started["run_id"])

                run_state = service.get_run(started["run_id"])
                started_event = next(event for event in run_state.get("events", []) if event.get("event_type") == "TOOL_EXECUTION_STARTED")
                self.assertEqual(started_event.get("payload", {}).get("risk_level"), "read_only")

                delete_request = ToolRequest(tool_id="filesystem", action="delete_file", parameters={"path": str(marker)})
                service.execute_tool_request(delete_request, run_id=started["run_id"])

                pending_events = [event for event in service.get_run(started["run_id"]).get("events", []) if event.get("event_type") == "TOOL_EXECUTION_PENDING_APPROVAL"]
                self.assertTrue(pending_events)
                self.assertEqual(pending_events[0].get("payload", {}).get("risk_level"), "destructive")
            finally:
                service.shutdown()

    def test_execute_tool_request_stops_when_run_is_cancelled(self):
        with tempfile.TemporaryDirectory(prefix="hermes-tool-cancel-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                workflow_path = Path(__file__).resolve().parent.parent / "examples" / "approval_example.json"
                started = service.start_run(str(workflow_path))
                self.assertEqual(started["status"], "WAITING_APPROVAL")
                cancelled = service.cancel_run(started["run_id"])
                self.assertTrue(cancelled["accepted"])

                request = ToolRequest(tool_id="filesystem", action="read_file", parameters={"path": "/tmp/does-not-matter"})
                envelopes = service.execute_tool_request(request, run_id=started["run_id"])
                self.assertEqual(len(envelopes), 1)
                self.assertEqual(envelopes[0].status, "error")
                self.assertIn("cancelled", envelopes[0].error.lower())
            finally:
                service.shutdown()


if __name__ == "__main__":
    unittest.main()
