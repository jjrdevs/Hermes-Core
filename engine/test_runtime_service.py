import tempfile
import time
import unittest
from pathlib import Path

from engine.runtime_service import RuntimeService


class TestRuntimeService(unittest.TestCase):
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
