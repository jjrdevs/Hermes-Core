import tempfile
import unittest
from pathlib import Path

from engine.models import Artifact, StepDefinition, WorkflowDefinition
from engine.runner import RuntimeRunner


class TestFailureHandling(unittest.TestCase):
    def test_worker_failure_updates_execution_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            runner = RuntimeRunner(event_db, artifact_db)
            step = StepDefinition(
                id="architect-1",
                role="architect",
                objective={"description": "Produce architecture"},
                depends_on=[],
                outputs=["architecture_v1"],
                constraints={},
            )
            workflow_definition = WorkflowDefinition.create(
                name="build_oauth",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )
            runner.kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = runner.kernel.start_workflow(workflow_definition.workflow_definition_id)
            step_execution_id = runner.kernel.schedule_next_step(workflow_execution_id)
            self.assertIsNotNone(step_execution_id)
            runner.kernel.assign_execution(
                step_execution_id,
                worker_assigned={"worker_type": step.role, "worker_id": "local-worker-1"},
                model_assigned={"adapter": "local", "model_name": "stub-model"},
            )
            runner.kernel.start_execution(step_execution_id)
            runner.kernel.fail_execution(step_execution_id, "simulated worker crash", failure_type="worker")
            execution = runner.kernel.step_executions[step_execution_id]
            self.assertEqual(execution.status, "FAILED")
            workflow_execution = runner.kernel.workflow_executions[workflow_execution_id]
            self.assertIn(step_execution_id, workflow_execution.failed_executions)
            runner.shutdown()

    def test_validation_failure_emits_failed_step_event(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            runner = RuntimeRunner(event_db, artifact_db)
            step = StepDefinition(
                id="architect-1",
                role="architect",
                objective={"description": "Produce architecture"},
                depends_on=[],
                outputs=["architecture_v1"],
                constraints={},
            )
            workflow_definition = WorkflowDefinition.create(
                name="build_oauth",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )
            runner.kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = runner.kernel.start_workflow(workflow_definition.workflow_definition_id)
            step_execution_id = runner.kernel.schedule_next_step(workflow_execution_id)
            self.assertIsNotNone(step_execution_id)
            runner.kernel.assign_execution(
                step_execution_id,
                worker_assigned={"worker_type": step.role, "worker_id": "local-worker-1"},
                model_assigned={"adapter": "local", "model_name": "stub-model"},
            )
            runner.kernel.start_execution(step_execution_id)
            bad_artifact = Artifact.create(
                artifact_type="wrong_type",
                title="wrong_output",
                content={"text": "bad"},
                created_by=step.role,
                inputs=[],
                status="CREATED",
                decision_record={"reason": "bad output"},
                metadata={},
            )
            with self.assertRaises(ValueError):
                runner.kernel.complete_execution(step_execution_id, [bad_artifact])
            execution = runner.kernel.step_executions[step_execution_id]
            self.assertEqual(execution.status, "FAILED")
            workflow_execution = runner.kernel.workflow_executions[workflow_execution_id]
            self.assertIn(step_execution_id, workflow_execution.failed_executions)
            runner.shutdown()
