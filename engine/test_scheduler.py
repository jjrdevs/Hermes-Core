import tempfile
import unittest
from pathlib import Path

from engine.models import Artifact, StepDefinition, WorkflowDefinition
from engine.runner import RuntimeRunner
from engine.runtime import RuntimeKernel
from engine.scheduler import BackgroundScheduler, LocalLeaseLockBackend, SQLiteJobStore


class TestScheduler(unittest.TestCase):
    def test_scheduler_finds_first_ready_step(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            runner = RuntimeRunner(event_db, artifact_db)
            step1 = StepDefinition(
                id="s1",
                role="architect",
                objective={"description": "architect"},
                depends_on=[],
                outputs=["a1"],
                constraints={},
            )
            step2 = StepDefinition(
                id="s2",
                role="developer",
                objective={"description": "developer"},
                depends_on=["s1"],
                outputs=["a2"],
                constraints={},
            )
            workflow_definition = WorkflowDefinition.create(
                name="test",
                steps=[step1, step2],
                transitions=[],
                policy_refs=[],
            )
            workflow_execution_id = runner.run_workflow(workflow_definition)
            workflow_execution = runner.kernel.workflow_executions[workflow_execution_id]
            self.assertEqual(workflow_execution.status, "COMPLETED")
            self.assertEqual(len(workflow_execution.completed_executions), 2)
            self.assertEqual(len(runner.kernel.artifact_store.list()), 2)
            runner.shutdown()

    def test_scheduler_respects_schedule_transitions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            kernel = RuntimeKernel(event_db, artifact_db)

            step1 = StepDefinition(
                id="architect-1",
                role="architect",
                objective={"description": "Design architecture"},
                depends_on=[],
                outputs=["architecture_v1"],
                constraints={},
            )
            step2 = StepDefinition(
                id="developer-1",
                role="developer",
                objective={"description": "Implement architecture"},
                depends_on=["architect-1"],
                outputs=["implementation_patch_v1"],
                constraints={},
            )
            step3 = StepDefinition(
                id="qa-1",
                role="qa",
                objective={"description": "Validate implementation"},
                depends_on=["architect-1"],
                outputs=["qa_report_v1"],
                constraints={},
            )
            workflow_definition = WorkflowDefinition.create(
                name="transition_schedule_workflow",
                steps=[step1, step2, step3],
                transitions=[
                    {
                        "priority": 100,
                        "condition": {
                            "event": "STEP_COMPLETED",
                            "artifact_type": "architecture_v1",
                        },
                        "action": {
                            "schedule": {"role": "developer"},
                        },
                    }
                ],
                policy_refs=[],
            )
            kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)
            step_execution_id = kernel.schedule_next_step(workflow_execution_id)
            self.assertIsNotNone(step_execution_id)
            kernel.assign_execution(
                step_execution_id,
                worker_assigned={"worker_type": "architect", "worker_id": "local-worker-1"},
                model_assigned={"adapter": "local", "model_name": "stub-model"},
            )
            kernel.start_execution(step_execution_id)
            artifact = Artifact.create(
                artifact_type="architecture_v1",
                title="architecture_artifact",
                content={"architecture": "ok"},
                created_by="architect",
                inputs=[],
                status="CREATED",
                decision_record={"reason": "design_complete"},
                metadata={},
            )
            kernel.complete_execution(step_execution_id, [artifact])
            next_step_id = kernel.schedule_next_step(workflow_execution_id)
            self.assertIsNotNone(next_step_id)
            self.assertEqual(kernel.step_executions[next_step_id].step_id, "developer-1")
            kernel.shutdown()

    def test_scheduler_blocks_when_require_approval_transition_applies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            kernel = RuntimeKernel(event_db, artifact_db)

            step1 = StepDefinition(
                id="architect-1",
                role="architect",
                objective={"description": "Design architecture"},
                depends_on=[],
                outputs=["approval_artifact"],
                constraints={},
            )
            step2 = StepDefinition(
                id="request-approval",
                role="human_operator",
                objective={"description": "Approve architecture"},
                depends_on=["architect-1"],
                outputs=["approval_decision"],
                constraints={},
            )
            workflow_definition = WorkflowDefinition.create(
                name="transition_approval_workflow",
                steps=[step1, step2],
                transitions=[
                    {
                        "priority": 100,
                        "condition": {
                            "event": "STEP_COMPLETED",
                            "artifact_type": "approval_artifact",
                        },
                        "action": {
                            "require_approval": {"role": "human_operator"},
                        },
                    }
                ],
                policy_refs=[],
            )
            kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)
            step_execution_id = kernel.schedule_next_step(workflow_execution_id)
            self.assertEqual(kernel.step_executions[step_execution_id].step_id, "architect-1")
            kernel.assign_execution(
                step_execution_id,
                worker_assigned={"worker_type": "architect", "worker_id": "local-worker-1"},
                model_assigned={"adapter": "local", "model_name": "stub-model"},
            )
            kernel.start_execution(step_execution_id)
            artifact = Artifact.create(
                artifact_type="approval_artifact",
                title="approval_artifact",
                content={"approval": "pending"},
                created_by="architect",
                inputs=[],
                status="CREATED",
                decision_record={"reason": "design_complete"},
                metadata={},
            )
            kernel.complete_execution(step_execution_id, [artifact])
            self.assertIsNone(kernel.schedule_next_step(workflow_execution_id))
            kernel.shutdown()

    def test_scheduler_deterministic_same_priority_transitions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            kernel = RuntimeKernel(event_db, artifact_db)

            step1 = StepDefinition(
                id="architect-1",
                role="architect",
                objective={"description": "Design architecture"},
                depends_on=[],
                outputs=["architecture_v1"],
                constraints={},
            )
            step2 = StepDefinition(
                id="developer-1",
                role="developer",
                objective={"description": "Implement architecture"},
                depends_on=["architect-1"],
                outputs=["implementation_patch_v1"],
                constraints={},
            )
            step3 = StepDefinition(
                id="qa-1",
                role="qa",
                objective={"description": "Validate implementation"},
                depends_on=["architect-1"],
                outputs=["qa_report_v1"],
                constraints={},
            )
            workflow_definition = WorkflowDefinition.create(
                name="deterministic_transition_workflow",
                steps=[step1, step2, step3],
                transitions=[
                    {
                        "priority": 100,
                        "condition": {
                            "event": "STEP_COMPLETED",
                            "artifact_type": "architecture_v1",
                        },
                        "action": {
                            "schedule": {"role": "developer"},
                        },
                    },
                    {
                        "priority": 100,
                        "condition": {
                            "event": "STEP_COMPLETED",
                            "artifact_type": "architecture_v1",
                        },
                        "action": {
                            "schedule": {"role": "qa"},
                        },
                    },
                ],
                policy_refs=[],
            )
            kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)
            step_execution_id = kernel.schedule_next_step(workflow_execution_id)
            kernel.assign_execution(
                step_execution_id,
                worker_assigned={"worker_type": "architect", "worker_id": "local-worker-1"},
                model_assigned={"adapter": "local", "model_name": "stub-model"},
            )
            kernel.start_execution(step_execution_id)
            artifact = Artifact.create(
                artifact_type="architecture_v1",
                title="architecture_artifact",
                content={"architecture": "ok"},
                created_by="architect",
                inputs=[],
                status="CREATED",
                decision_record={"reason": "design_complete"},
                metadata={},
            )
            kernel.complete_execution(step_execution_id, [artifact])
            next_step_id = kernel.schedule_next_step(workflow_execution_id)
            self.assertIsNotNone(next_step_id)
            self.assertEqual(kernel.step_executions[next_step_id].step_id, "developer-1")
            kernel.shutdown()

    def test_scheduler_accepts_an_injected_lock_backend(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            job_store = SQLiteJobStore(Path(temp_dir) / "jobs.db")

            class RecordingLockBackend(LocalLeaseLockBackend):
                def __init__(self, root_path):
                    super().__init__(root_path)
                    self.acquisitions = []
                    self.releases = []

                def acquire(self, job_id, owner_id, lease_seconds=60):
                    self.acquisitions.append((job_id, owner_id, lease_seconds))
                    return super().acquire(job_id, owner_id, lease_seconds=lease_seconds)

                def release(self, job_id, owner_id):
                    self.releases.append((job_id, owner_id))
                    return super().release(job_id, owner_id)

            backend = RecordingLockBackend(Path(temp_dir))
            scheduler = BackgroundScheduler(job_store, lock_backend=backend)
            job = scheduler.create_job("run a maintenance task", runtime_budget_seconds=5)

            result = scheduler.start_job(
                job["job_id"],
                lambda payload: {"status": "COMPLETED", "task_status": "COMPLETED_VERIFIED", "status_schema_version": 1, "summary": {"ok": True}},
            )

            self.assertEqual(result["execution_status"]["status"], "COMPLETED")
            self.assertEqual(backend.acquisitions[0][0], job["job_id"])
            self.assertEqual(backend.releases[0][0], job["job_id"])
            self.assertEqual(result["execution_status"]["lock"]["owner_id"], backend.acquisitions[0][1])
            job_store.close()

    def test_scheduler_cannot_schedule_after_workflow_complete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            kernel = RuntimeKernel(event_db, artifact_db)

            step = StepDefinition(
                id="s1",
                role="architect",
                objective={"description": "architect"},
                depends_on=[],
                outputs=["artifact_a1"],
                constraints={},
            )
            workflow_definition = WorkflowDefinition.create(
                name="completed_workflow",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )
            kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)
            step_execution_id = kernel.schedule_next_step(workflow_execution_id)
            kernel.assign_execution(
                step_execution_id,
                worker_assigned={"worker_type": "architect", "worker_id": "local-worker-1"},
                model_assigned={"adapter": "local", "model_name": "stub-model"},
            )
            kernel.start_execution(step_execution_id)
            artifact = Artifact.create(
                artifact_type="artifact_a1",
                title="artifact_a1",
                content={"data": "done"},
                created_by="architect",
                inputs=[],
                status="CREATED",
                decision_record={},
                metadata={},
            )
            kernel.complete_execution(step_execution_id, [artifact])
            kernel.complete_workflow(workflow_execution_id)
            self.assertTrue(kernel.workflow_complete(workflow_execution_id))
            self.assertIsNone(kernel.schedule_next_step(workflow_execution_id))
            kernel.shutdown()
