import tempfile
import unittest
from pathlib import Path

from engine.models import Artifact, StepDefinition, WorkflowDefinition
from engine.runner import RuntimeRunner
from engine.runtime import RuntimeKernel


class TestStepValidation(unittest.TestCase):
    def test_valid_step_output_passes_validation(self):
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

            workflow_execution_id = runner.run_workflow(workflow_definition)
            workflow_execution = runner.kernel.workflow_executions[workflow_execution_id]
            self.assertEqual(workflow_execution.status, "COMPLETED")
            self.assertEqual(len(runner.kernel.artifact_store.list()), 1)
            runner.shutdown()

    def test_invalid_step_output_fails_validation(self):
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
            step_execution_id = runner.kernel.create_step_execution(workflow_execution_id, step)
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
            runner.shutdown()

    def test_artifact_version_lineage_persists_across_replay(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            runner = RuntimeRunner(event_db, artifact_db)
            step = StepDefinition(
                id="architect-1",
                role="architect",
                objective={"description": "Produce architecture"},
                depends_on=[],
                outputs=["architecture_v1", "architecture_v2"],
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
            step_execution_id = runner.kernel.create_step_execution(workflow_execution_id, step)
            runner.kernel.assign_execution(
                step_execution_id,
                worker_assigned={"worker_type": step.role, "worker_id": "local-worker-1"},
                model_assigned={"adapter": "local", "model_name": "stub-model"},
            )
            runner.kernel.start_execution(step_execution_id)
            artifact_v1 = Artifact.create(
                artifact_type="architecture",
                title="architecture_v1",
                content={"description": "design v1"},
                created_by=step.role,
                artifact_id="architecture_oauth",
                inputs=[],
                parent_version=None,
                status="CREATED",
                decision_record={"reason": "initial draft"},
                metadata={"note": "v1"},
            )
            artifact_v2 = Artifact.create(
                artifact_type="architecture",
                title="architecture_v2",
                content={"description": "design v1"},
                created_by=step.role,
                artifact_id="architecture_oauth",
                inputs=[artifact_v1.artifact_id],
                parent_version=artifact_v1.version,
                status="CREATED",
                decision_record={"reason": "revised draft"},
                metadata={"note": "v2"},
            )
            self.assertEqual(artifact_v1.artifact_id, artifact_v2.artifact_id)
            self.assertEqual(artifact_v1.version + 1, artifact_v2.version)
            self.assertEqual(artifact_v2.parent_version, artifact_v1.version)
            self.assertEqual(artifact_v1.content_hash, artifact_v2.content_hash)
            runner.kernel.complete_execution(step_execution_id, [artifact_v1, artifact_v2])
            runner.shutdown()

            recovered_kernel = RuntimeKernel(event_db, artifact_db)
            artifacts = recovered_kernel.artifact_store.list()
            self.assertEqual(len(artifacts), 2)
            retrieved_v2 = recovered_kernel.artifact_store.get("architecture_oauth")
            self.assertIsNotNone(retrieved_v2)
            self.assertEqual(retrieved_v2.version, 2)
            self.assertEqual(retrieved_v2.parent_version, 1)
            self.assertEqual(retrieved_v2.content_hash, artifact_v2.content_hash)
            self.assertNotEqual(retrieved_v2.artifact_hash, artifact_v1.artifact_hash)
            recovered_kernel.shutdown()
