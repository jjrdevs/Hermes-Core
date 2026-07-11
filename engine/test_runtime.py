import subprocess
import tempfile
import unittest
from pathlib import Path

import unittest.mock as mock

from engine.models import (
    Artifact,
    CapabilityRequest,
    Event,
    ExecutionContext,
    StepDefinition,
    Tool,
    ToolRequest,
    WorkflowDefinition,
    WorkerResponse,
    _now_iso,
)
from engine.capability import CapabilityRegistry
from engine.runner import RuntimeRunner
from engine.runtime import RuntimeKernel
from workers.model_adapter import StubModelAdapter


class TestRuntimeRunner(unittest.TestCase):
    def test_runtime_runner_executes_first_step_and_stores_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            runner = RuntimeRunner(event_db, artifact_db)
            step = StepDefinition(
                id="architect-1",
                role="architect",
                objective={
                    "description": "Design the OAuth architecture",
                    "acceptance_criteria": ["Supports refresh tokens"],
                },
                depends_on=[],
                outputs=["architecture_v1"],
                constraints={"allowed_tools": ["filesystem"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="build_oauth",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )

            workflow_execution_id = runner.run_workflow(workflow_definition)
            self.assertIn(workflow_execution_id, runner.kernel.workflow_executions)
            workflow_execution = runner.kernel.workflow_executions[workflow_execution_id]
            self.assertEqual(workflow_execution.status, "COMPLETED")
            self.assertTrue(workflow_execution.completed_executions)
            self.assertTrue(workflow_execution.produced_artifacts)
            self.assertEqual(len(runner.kernel.artifact_store.list()), 1)
            runner.shutdown()

    def test_execution_context_persists_and_replays(self):
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
                constraints={"allowed_tools": ["filesystem"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="execution_context_workflow",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )
            execution_context = ExecutionContext(
                execution_mode="interactive",
                policy_profile="default",
                sandbox_profile="standard",
                approval_requirements={"human_confirm": True},
                resource_constraints={"max_parallel_steps": 1},
                environment_metadata={"workspace": "local"},
            )
            kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = kernel.start_workflow(
                workflow_definition.workflow_definition_id,
                execution_context=execution_context,
            )
            kernel.shutdown()

            recovered_kernel = RuntimeKernel(event_db, artifact_db)
            recovered_execution = recovered_kernel.workflow_executions[workflow_execution_id]
            self.assertEqual(recovered_execution.execution_context, execution_context)
            created_event = next(
                event for event in recovered_kernel.event_log.all_events() if event.event_type == "WORKFLOW_CREATED"
            )
            self.assertEqual(created_event.payload["execution_context"], execution_context.to_dict())
            recovered_kernel.shutdown()

    def test_assign_execution_receives_execution_context_in_capability_profile(self):
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
                constraints={"allowed_tools": ["filesystem"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="capability_context_workflow",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )
            execution_context = ExecutionContext(
                execution_mode="autonomous",
                policy_profile="default",
                sandbox_profile="standard",
                approval_requirements={"human_confirm": False},
                resource_constraints={"max_parallel_steps": 3},
                environment_metadata={"workspace": "local"},
            )
            kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = kernel.start_workflow(
                workflow_definition.workflow_definition_id,
                execution_context=execution_context,
            )
            step_execution_id = kernel.schedule_next_step(workflow_execution_id)

            decision = kernel.assign_execution(step_execution_id, model_adapter=StubModelAdapter())
            self.assertTrue(decision.allowed)
            assigned_event = next(
                event for event in kernel.event_log.all_events() if event.event_type == "STEP_EXECUTION_ASSIGNED"
            )

            worker_profile = assigned_event.payload["worker_assigned"]["profile"]
            model_profile = assigned_event.payload["model_assigned"]["profile"]
            self.assertEqual(worker_profile["execution_context"], execution_context.to_dict())
            self.assertEqual(model_profile["execution_context"], execution_context.to_dict())
            kernel.shutdown()

    def test_runtime_kernel_recovers_definitions_and_events_on_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            runner = RuntimeRunner(event_db, artifact_db)

            step1 = StepDefinition(
                id="s1",
                role="architect",
                objective={"description": "architect"},
                depends_on=[],
                outputs=["artifact_a1"],
                constraints={"allowed_tools": ["filesystem"]},
            )
            step2 = StepDefinition(
                id="s2",
                role="developer",
                objective={"description": "developer"},
                depends_on=["s1"],
                outputs=["artifact_a2"],
                constraints={"allowed_tools": ["filesystem"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="recoverable_workflow",
                steps=[step1, step2],
                transitions=[],
                policy_refs=[],
            )

            workflow_execution_id = runner.run_workflow(workflow_definition)
            original_execution = runner.kernel.workflow_executions[workflow_execution_id]
            original_artifacts = sorted(a.artifact_id for a in runner.kernel.artifact_store.list())
            original_steps = sorted(runner.kernel.step_executions.keys())
            runner.shutdown()

            recovered_kernel = RuntimeKernel(event_db, artifact_db)
            self.assertIn(workflow_definition.workflow_definition_id, recovered_kernel.workflow_definitions)
            self.assertIn(workflow_execution_id, recovered_kernel.workflow_executions)

            recovered_execution = recovered_kernel.workflow_executions[workflow_execution_id]
            self.assertEqual(recovered_execution.status, original_execution.status)
            self.assertEqual(recovered_execution.workflow_definition_hash, original_execution.workflow_definition_hash)
            self.assertEqual(recovered_execution.workflow_definition_version, original_execution.workflow_definition_version)
            self.assertEqual(sorted(recovered_kernel.step_executions.keys()), original_steps)
            self.assertEqual(len(recovered_kernel.artifact_store.list()), len(original_artifacts))
            self.assertEqual(
                sorted(a.artifact_id for a in recovered_kernel.artifact_store.list()),
                original_artifacts,
            )
            self.assertTrue(all(execution.status == "COMPLETED" for execution in recovered_kernel.step_executions.values()))
            recovered_kernel.shutdown()

    def test_workflow_definition_persistence_survives_restart_and_matches_hash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            workflow_definition_db = Path(temp_dir) / "workflow_definitions.db"
            kernel = RuntimeKernel(event_db, artifact_db, workflow_definition_db)

            step = StepDefinition(
                id="s1",
                role="architect",
                objective={"description": "architect"},
                depends_on=[],
                outputs=["artifact_a1"],
                constraints={},
            )
            workflow_definition = WorkflowDefinition.create(
                name="definition_persistence_workflow",
                steps=[step],
                transitions=[],
                policy_refs=["policy_1"],
            )
            kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)
            kernel.shutdown()

            recovered_kernel = RuntimeKernel(event_db, artifact_db, workflow_definition_db)
            recovered_definition = recovered_kernel.workflow_definitions[workflow_definition.workflow_definition_id]
            self.assertEqual(recovered_definition.compute_definition_hash(), workflow_definition.compute_definition_hash())
            self.assertEqual(recovered_definition.definition_version, workflow_definition.definition_version)
            self.assertEqual(recovered_definition.to_dict(), workflow_definition.to_dict())
            recovered_execution = recovered_kernel.workflow_executions[workflow_execution_id]
            self.assertEqual(recovered_execution.workflow_definition_hash, workflow_definition.compute_definition_hash())
            recovered_kernel.shutdown()

    def test_tool_registry_persists_tool_metadata_across_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            kernel = RuntimeKernel(event_db, artifact_db)

            tool = Tool(
                tool_id="filesystem",
                name="Filesystem Tool",
                description="Access files",
                actions=["read", "write"],
                allowed_roles=["architect", "developer"],
                metadata={"version": "1.0"},
            )
            kernel.register_tool(tool)
            kernel.shutdown()

            recovered_kernel = RuntimeKernel(event_db, artifact_db)
            recovered_tool = recovered_kernel.tool_registry.get("filesystem")
            self.assertIsNotNone(recovered_tool)
            self.assertEqual(recovered_tool.compute_tool_hash(), tool.compute_tool_hash())
            self.assertEqual(recovered_tool.to_dict(), tool.to_dict())
            recovered_kernel.shutdown()

    def test_duplicate_event_replay_does_not_corrupt_state(self):
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
                name="duplicate_replay_workflow",
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
            events = kernel.event_log.all_events()
            self.assertEqual(len(events), len(set(event.event_id for event in events)))
            kernel.shutdown()

            recovered_kernel = RuntimeKernel(event_db, artifact_db)
            self.assertEqual(
                recovered_kernel.workflow_executions[workflow_execution_id].status,
                kernel.workflow_executions[workflow_execution_id].status,
            )
            self.assertEqual(
                recovered_kernel.step_executions[step_execution_id].status,
                kernel.step_executions[step_execution_id].status,
            )
            recovered_kernel.shutdown()

    def test_unknown_event_types_do_not_break_recovery(self):
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
                name="future_event_workflow",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )
            kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)
            unknown_event = Event.create(
                event_type="FUTURE_UNKNOWN_EVENT",
                workflow_id=workflow_definition.workflow_id,
                execution_id=workflow_execution_id,
                payload={"reason": "future compatibility"},
            )
            kernel.event_log.append(unknown_event)
            kernel.shutdown()

            recovered_kernel = RuntimeKernel(event_db, artifact_db)
            self.assertIn(workflow_execution_id, recovered_kernel.workflow_executions)
            self.assertEqual(recovered_kernel.workflow_executions[workflow_execution_id].status, "PLANNING")
            recovered_kernel.shutdown()

    def test_artifact_version_and_lineage_persist_across_restart(self):
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
                name="artifact_version_workflow",
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
            base_artifact = Artifact.create(
                artifact_type="artifact_a1",
                title="artifact_a1",
                content={"data": "version1"},
                created_by="architect",
                inputs=[],
                status="CREATED",
                decision_record={},
                metadata={},
            )
            artifact_event = Event.create(
                event_type="ARTIFACT_CREATED",
                workflow_id=workflow_execution_id,
                execution_id=step_execution_id,
                payload={
                    "artifact_id": base_artifact.artifact_id,
                    "artifact_type": base_artifact.artifact_type,
                    "version": base_artifact.version,
                    "title": base_artifact.title,
                    "content": base_artifact.content,
                    "content_hash": base_artifact.content_hash,
                    "artifact_hash": base_artifact.artifact_hash,
                    "created_by": base_artifact.created_by,
                    "created_at": base_artifact.created_at,
                    "inputs": base_artifact.inputs,
                    "parent_version": base_artifact.parent_version,
                    "status": base_artifact.status,
                    "decision_record": base_artifact.decision_record,
                    "metadata": base_artifact.metadata,
                },
            )
            kernel.event_log.append(artifact_event)
            kernel._apply_event(artifact_event)

            version_two = Artifact.create(
                artifact_type="artifact_a1",
                title="artifact_a1",
                content={"data": "version2"},
                created_by="architect",
                artifact_id=base_artifact.artifact_id,
                inputs=[],
                parent_version=base_artifact.version,
                status="CREATED",
                decision_record={},
                metadata={},
            )
            artifact_event_v2 = Event.create(
                event_type="ARTIFACT_CREATED",
                workflow_id=workflow_execution_id,
                execution_id=step_execution_id,
                payload={
                    "artifact_id": version_two.artifact_id,
                    "artifact_type": version_two.artifact_type,
                    "version": version_two.version,
                    "title": version_two.title,
                    "content": version_two.content,
                    "content_hash": version_two.content_hash,
                    "artifact_hash": version_two.artifact_hash,
                    "created_by": version_two.created_by,
                    "created_at": version_two.created_at,
                    "inputs": version_two.inputs,
                    "parent_version": version_two.parent_version,
                    "status": version_two.status,
                    "decision_record": version_two.decision_record,
                    "metadata": version_two.metadata,
                },
            )
            kernel.event_log.append(artifact_event_v2)
            kernel._apply_event(artifact_event_v2)
            kernel.shutdown()

            recovered_kernel = RuntimeKernel(event_db, artifact_db)
            recovered_latest = recovered_kernel.artifact_store.get(base_artifact.artifact_id)
            recovered_v1 = recovered_kernel.artifact_store.get(base_artifact.artifact_id, 1)
            recovered_v2 = recovered_kernel.artifact_store.get(base_artifact.artifact_id, 2)
            self.assertIsNotNone(recovered_latest)
            self.assertEqual(recovered_latest.version, 2)
            self.assertIsNotNone(recovered_v1)
            self.assertIsNotNone(recovered_v2)
            self.assertEqual(recovered_v1.parent_version, None)
            self.assertEqual(recovered_v2.parent_version, 1)
            recovered_kernel.shutdown()

    def test_runtime_kernel_can_start_two_executions_from_same_definition(self):
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
                constraints={"allowed_tools": ["filesystem"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="shared_definition",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )

            kernel.register_workflow_definition(workflow_definition)
            first_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)
            second_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)

            self.assertNotEqual(first_execution_id, second_execution_id)
            self.assertEqual(
                kernel.workflow_executions[first_execution_id].workflow_definition_id,
                kernel.workflow_executions[second_execution_id].workflow_definition_id,
            )
            self.assertEqual(
                kernel.workflow_executions[first_execution_id].workflow_definition_hash,
                kernel.workflow_executions[second_execution_id].workflow_definition_hash,
            )
            self.assertEqual(
                kernel.workflow_executions[first_execution_id].workflow_id,
                kernel.workflow_executions[second_execution_id].workflow_id,
            )
            kernel.shutdown()

    def test_runtime_runner_rejects_conflicting_external_definition(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            runner = RuntimeRunner(event_db, artifact_db)

            step = StepDefinition(
                id="architect-1",
                role="architect",
                objective={"description": "Design architecture"},
                depends_on=[],
                outputs=["architecture_v1"],
                constraints={"allowed_tools": ["filesystem"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="build_oauth",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )
            runner.kernel.register_workflow_definition(workflow_definition)

            modified_definition = WorkflowDefinition(
                workflow_definition_id=workflow_definition.workflow_definition_id,
                workflow_id=workflow_definition.workflow_id,
                name=workflow_definition.name,
                created_at=workflow_definition.created_at,
                steps=[
                    StepDefinition(
                        id="architect-1",
                        role="architect",
                        objective={"description": "Different objective"},
                        depends_on=[],
                        outputs=["architecture_v1"],
                        constraints={"allowed_tools": ["filesystem"]},
                    )
                ],
                transitions=workflow_definition.transitions,
                policy_refs=workflow_definition.policy_refs,
                definition_version=workflow_definition.definition_version,
            )

            with self.assertRaises(ValueError):
                runner.kernel.register_workflow_definition(modified_definition)
            runner.shutdown()

    def test_policy_denied_step_never_invokes_worker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            runner = RuntimeRunner(event_db, artifact_db)

            step = StepDefinition(
                id="architect-1",
                role="architect",
                objective={"description": "Design the OAuth architecture"},
                depends_on=[],
                outputs=["architecture_v1"],
                constraints={"allow_execution": False},
            )
            workflow_definition = WorkflowDefinition.create(
                name="build_oauth",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )

            with mock.patch("workers.local_worker.LocalWorker.execute") as execute_mock:
                with self.assertRaises(RuntimeError):
                    runner.run_workflow(workflow_definition)
                execute_mock.assert_not_called()
            runner.shutdown()

    def test_unauthorized_tool_request_is_denied(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            kernel = RuntimeKernel(event_db, artifact_db)

            step = StepDefinition(
                id="architect-1",
                role="architect",
                objective={"description": "Design architecture"},
                depends_on=[],
                outputs=["architecture_v1"],
                constraints={"allowed_tools": ["filesystem"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="build_oauth",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )
            kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)
            step_execution_id = kernel.schedule_next_step(workflow_execution_id)
            kernel.assign_execution(
                step_execution_id,
                worker_assigned={"worker_type": step.role, "worker_id": "local-worker-1"},
                model_assigned={"adapter": "local", "model_name": "stub-model"},
            )
            kernel.start_execution(step_execution_id)
            tool = Tool(
                tool_id="filesystem",
                name="Filesystem Tool",
                description="Access files",
                actions=["read"],
                allowed_roles=["developer"],
                metadata={},
            )
            kernel.tool_registry.register(tool)

            decision = kernel.request_tool(step_execution_id, "filesystem", "read", {"path": "/tmp"})
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.reason, "tool_registry.unauthorized")
            self.assertTrue(any(event.event_type == "TOOL_FAILED" for event in kernel.event_log.all_events()))
            kernel.shutdown()

    def test_authorized_tool_request_emits_requested_and_invoked_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            kernel = RuntimeKernel(event_db, artifact_db)

            step = StepDefinition(
                id="architect-1",
                role="architect",
                objective={"description": "Design architecture"},
                depends_on=[],
                outputs=["architecture_v1"],
                constraints={"allowed_tools": ["filesystem"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="build_oauth",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )
            kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)
            step_execution_id = kernel.schedule_next_step(workflow_execution_id)
            kernel.assign_execution(
                step_execution_id,
                worker_assigned={"worker_type": step.role, "worker_id": "local-worker-1"},
                model_assigned={"adapter": "local", "model_name": "stub-model"},
            )
            kernel.start_execution(step_execution_id)
            tool = Tool(
                tool_id="filesystem",
                name="Filesystem Tool",
                description="Access files",
                actions=["read"],
                allowed_roles=["architect"],
                metadata={},
            )
            kernel.tool_registry.register(tool)

            decision = kernel.request_tool(step_execution_id, "filesystem", "read", {"path": "/tmp"})
            self.assertTrue(decision.allowed)
            events = kernel.event_log.all_events()
            self.assertTrue(any(event.event_type == "TOOL_REQUESTED" for event in events))
            self.assertTrue(any(event.event_type == "TOOL_INVOKED" for event in events))
            kernel.shutdown()

    def test_workflow_enters_executing_after_step_start(self):
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
                constraints={"allowed_tools": ["filesystem"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="status_workflow",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )
            kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)
            self.assertEqual(kernel.get_workflow_status(workflow_execution_id), "PLANNING")
            step_execution_id = kernel.schedule_next_step(workflow_execution_id)
            kernel.assign_execution(
                step_execution_id,
                worker_assigned={"worker_type": "architect", "worker_id": "local-worker-1"},
                model_assigned={"adapter": "local", "model_name": "stub-model"},
            )
            kernel.start_execution(step_execution_id)
            self.assertEqual(kernel.get_workflow_status(workflow_execution_id), "EXECUTING")
            kernel.shutdown()

    def test_tool_request_executes_filesystem_tool_and_records_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            kernel = RuntimeKernel(event_db, artifact_db)
            workspace_dir = Path(temp_dir) / "workspace"
            workspace_dir.mkdir()
            target_path = workspace_dir / "notes.txt"
            target_path.write_text("hello hermes", encoding="utf-8")

            step = StepDefinition(
                id="architect-1",
                role="architect",
                objective={"description": "Inspect workspace"},
                depends_on=[],
                outputs=[],
                constraints={"allowed_tools": ["filesystem"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="filesystem_tool_workflow",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )
            kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)
            step_execution_id = kernel.schedule_next_step(workflow_execution_id)
            kernel.assign_execution(
                step_execution_id,
                worker_assigned={"worker_type": step.role, "worker_id": "local-worker-1"},
                model_assigned={"adapter": "local", "model_name": "stub-model"},
            )
            kernel.start_execution(step_execution_id)

            tool = Tool(
                tool_id="filesystem",
                name="Filesystem Tool",
                description="Access files",
                actions=["read", "write"],
                allowed_roles=["architect"],
                metadata={"kind": "filesystem", "allowed_roots": [str(workspace_dir)]},
            )
            kernel.register_tool(tool)

            decision = kernel.request_tool(step_execution_id, "filesystem", "read", {"path": str(target_path)})
            self.assertTrue(decision.allowed)
            invoke_event = next(event for event in kernel.event_log.all_events() if event.event_type == "TOOL_INVOKED")
            self.assertEqual(invoke_event.payload["result"]["status"], "ok")
            self.assertEqual(invoke_event.payload["result"]["data"]["content"], "hello hermes")
            kernel.shutdown()

    def test_shell_tool_executes_authorized_command_and_records_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            kernel = RuntimeKernel(event_db, artifact_db)

            step = StepDefinition(
                id="shell-1",
                role="architect",
                objective={"description": "Run a validation command"},
                depends_on=[],
                outputs=[],
                constraints={"allowed_tools": ["shell"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="shell_tool_workflow",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )
            kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)
            step_execution_id = kernel.schedule_next_step(workflow_execution_id)
            kernel.assign_execution(
                step_execution_id,
                worker_assigned={"worker_type": step.role, "worker_id": "local-worker-1"},
                model_assigned={"adapter": "local", "model_name": "stub-model"},
            )
            kernel.start_execution(step_execution_id)
            tool = Tool(
                tool_id="shell",
                name="Shell Tool",
                description="Run shell commands",
                actions=["exec"],
                allowed_roles=["architect"],
                metadata={"kind": "shell", "allowed_commands": ["python"]},
            )
            kernel.register_tool(tool)

            decision = kernel.request_tool(
                step_execution_id,
                "shell",
                "exec",
                {"command": ["python", "-c", "print('hello-shell')"]},
            )
            self.assertTrue(decision.allowed)
            invoke_event = next(event for event in kernel.event_log.all_events() if event.event_type == "TOOL_INVOKED")
            self.assertEqual(invoke_event.payload["result"]["status"], "ok")
            self.assertEqual(invoke_event.payload["result"]["data"]["exit_code"], 0)
            self.assertIn("hello-shell", invoke_event.payload["result"]["data"]["stdout"])
            self.assertEqual(invoke_event.payload["result"]["data"]["metadata"]["command"], ["python", "-c", "print('hello-shell')"])
            kernel.shutdown()

    def test_shell_tool_rejects_unauthorized_command_via_policy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            kernel = RuntimeKernel(event_db, artifact_db)

            step = StepDefinition(
                id="shell-2",
                role="architect",
                objective={"description": "Run a blocked command"},
                depends_on=[],
                outputs=[],
                constraints={"allowed_tools": ["shell"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="blocked_shell_tool_workflow",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )
            kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)
            step_execution_id = kernel.schedule_next_step(workflow_execution_id)
            kernel.assign_execution(
                step_execution_id,
                worker_assigned={"worker_type": step.role, "worker_id": "local-worker-1"},
                model_assigned={"adapter": "local", "model_name": "stub-model"},
            )
            kernel.start_execution(step_execution_id)
            tool = Tool(
                tool_id="shell",
                name="Shell Tool",
                description="Run shell commands",
                actions=["exec"],
                allowed_roles=["architect"],
                metadata={"kind": "shell", "allowed_commands": ["python"]},
            )
            kernel.register_tool(tool)

            decision = kernel.request_tool(step_execution_id, "shell", "exec", {"command": ["pytest", "-q"]})
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.reason, "tool_constraints.command_not_allowed")
            self.assertTrue(any(event.event_type == "STEP_EXECUTION_POLICY_DENIED" for event in kernel.event_log.all_events()))
            kernel.shutdown()

    def test_shell_tool_records_non_zero_exit_codes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            kernel = RuntimeKernel(event_db, artifact_db)

            step = StepDefinition(
                id="shell-3",
                role="architect",
                objective={"description": "Run a failing command"},
                depends_on=[],
                outputs=[],
                constraints={"allowed_tools": ["shell"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="failing_shell_tool_workflow",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )
            kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)
            step_execution_id = kernel.schedule_next_step(workflow_execution_id)
            kernel.assign_execution(
                step_execution_id,
                worker_assigned={"worker_type": step.role, "worker_id": "local-worker-1"},
                model_assigned={"adapter": "local", "model_name": "stub-model"},
            )
            kernel.start_execution(step_execution_id)
            tool = Tool(
                tool_id="shell",
                name="Shell Tool",
                description="Run shell commands",
                actions=["exec"],
                allowed_roles=["architect"],
                metadata={"kind": "shell", "allowed_commands": ["python"]},
            )
            kernel.register_tool(tool)

            decision = kernel.request_tool(step_execution_id, "shell", "exec", {"command": ["python", "-c", "import sys; sys.exit(7)"]})
            self.assertTrue(decision.allowed)
            invoke_event = next(event for event in kernel.event_log.all_events() if event.event_type == "TOOL_INVOKED")
            self.assertEqual(invoke_event.payload["result"]["status"], "error")
            self.assertEqual(invoke_event.payload["result"]["data"]["exit_code"], 7)
            self.assertFalse(invoke_event.payload["result"]["data"]["succeeded"])
            kernel.shutdown()

    def test_shell_tool_events_persist_across_runtime_recovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            kernel = RuntimeKernel(event_db, artifact_db)

            step = StepDefinition(
                id="shell-4",
                role="architect",
                objective={"description": "Run a persisted shell command"},
                depends_on=[],
                outputs=[],
                constraints={"allowed_tools": ["shell"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="shell_recovery_workflow",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )
            kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)
            step_execution_id = kernel.schedule_next_step(workflow_execution_id)
            kernel.assign_execution(
                step_execution_id,
                worker_assigned={"worker_type": step.role, "worker_id": "local-worker-1"},
                model_assigned={"adapter": "local", "model_name": "stub-model"},
            )
            kernel.start_execution(step_execution_id)
            tool = Tool(
                tool_id="shell",
                name="Shell Tool",
                description="Run shell commands",
                actions=["exec"],
                allowed_roles=["architect"],
                metadata={"kind": "shell", "allowed_commands": ["python"]},
            )
            kernel.register_tool(tool)
            kernel.request_tool(step_execution_id, "shell", "exec", {"command": ["python", "-c", "print('recovered')"]})
            kernel.shutdown()

            recovered_kernel = RuntimeKernel(event_db, artifact_db)
            invoke_events = [event for event in recovered_kernel.event_log.all_events() if event.event_type == "TOOL_INVOKED"]
            self.assertEqual(len(invoke_events), 1)
            self.assertEqual(invoke_events[0].payload["result"]["data"]["exit_code"], 0)
            self.assertIn("recovered", invoke_events[0].payload["result"]["data"]["stdout"])
            recovered_kernel.shutdown()

    def test_git_tool_executes_authorized_status_command_and_records_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            kernel = RuntimeKernel(event_db, artifact_db)
            repo_dir = Path(temp_dir) / "repo"
            repo_dir.mkdir()
            subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)
            (repo_dir / "notes.txt").write_text("hello git", encoding="utf-8")

            step = StepDefinition(
                id="git-1",
                role="developer",
                objective={"description": "Inspect repository status"},
                depends_on=[],
                outputs=[],
                constraints={"allowed_tools": ["git"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="git_tool_workflow",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )
            kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)
            step_execution_id = kernel.schedule_next_step(workflow_execution_id)
            kernel.assign_execution(
                step_execution_id,
                worker_assigned={"worker_type": step.role, "worker_id": "local-worker-1"},
                model_assigned={"adapter": "local", "model_name": "stub-model"},
            )
            kernel.start_execution(step_execution_id)
            tool = Tool(
                tool_id="git",
                name="Git Tool",
                description="Inspect git repositories",
                actions=["status"],
                allowed_roles=["developer"],
                metadata={"kind": "git", "allowed_actions": ["status"]},
            )
            kernel.register_tool(tool)

            decision = kernel.request_tool(step_execution_id, "git", "status", {"repo_path": str(repo_dir)})
            self.assertTrue(decision.allowed)
            invoke_event = next(event for event in kernel.event_log.all_events() if event.event_type == "TOOL_INVOKED")
            self.assertEqual(invoke_event.payload["result"]["status"], "ok")
            self.assertEqual(invoke_event.payload["result"]["data"]["exit_code"], 0)
            self.assertIn("notes.txt", invoke_event.payload["result"]["data"]["stdout"])
            kernel.shutdown()

    def test_git_tool_rejects_unauthorized_action_via_policy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            kernel = RuntimeKernel(event_db, artifact_db)

            step = StepDefinition(
                id="git-2",
                role="developer",
                objective={"description": "Inspect repository status"},
                depends_on=[],
                outputs=[],
                constraints={"allowed_tools": ["git"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="git_policy_workflow",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )
            kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)
            step_execution_id = kernel.schedule_next_step(workflow_execution_id)
            kernel.assign_execution(
                step_execution_id,
                worker_assigned={"worker_type": step.role, "worker_id": "local-worker-1"},
                model_assigned={"adapter": "local", "model_name": "stub-model"},
            )
            kernel.start_execution(step_execution_id)
            tool = Tool(
                tool_id="git",
                name="Git Tool",
                description="Inspect git repositories",
                actions=["status"],
                allowed_roles=["developer"],
                metadata={"kind": "git", "allowed_actions": ["status"]},
            )
            kernel.register_tool(tool)

            decision = kernel.request_tool(step_execution_id, "git", "diff", {"repo_path": str(temp_dir)})
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.reason, "tool_constraints.action_not_allowed")
            self.assertTrue(any(event.event_type == "STEP_EXECUTION_POLICY_DENIED" for event in kernel.event_log.all_events()))
            kernel.shutdown()

    def test_git_tool_supports_diff_commit_and_checkout_flow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            kernel = RuntimeKernel(event_db, artifact_db)
            repo_dir = Path(temp_dir) / "repo"
            repo_dir.mkdir()
            subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Hermes Bot"], cwd=repo_dir, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "hermes@example.com"], cwd=repo_dir, check=True, capture_output=True, text=True)
            (repo_dir / "notes.txt").write_text("hello git", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True, capture_output=True, text=True)
            (repo_dir / "notes.txt").write_text("hello git update", encoding="utf-8")

            step = StepDefinition(
                id="git-3",
                role="developer",
                objective={
                    "description": "Inspect and update repository state",
                    "required_capabilities": ["code", "reasoning"],
                    "tool_support": True,
                },
                depends_on=[],
                outputs=[],
                constraints={"allowed_tools": ["git"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="git_capability_workflow",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )
            kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)
            step_execution_id = kernel.schedule_next_step(workflow_execution_id)
            kernel.assign_execution(
                step_execution_id,
                worker_assigned={"worker_type": step.role, "worker_id": "local-worker-1"},
                model_assigned={"adapter": "local", "model_name": "stub-model"},
            )
            kernel.start_execution(step_execution_id)
            tool = Tool(
                tool_id="git",
                name="Git Tool",
                description="Inspect and mutate git repositories through Hermes",
                actions=["status", "diff", "commit", "checkout", "inspect"],
                allowed_roles=["developer"],
                metadata={
                    "kind": "git",
                    "allowed_actions": ["status", "diff", "commit", "checkout", "inspect"],
                    "capabilities": ["code", "reasoning", "tool_usage"],
                    "tool_support": True,
                },
            )
            kernel.register_tool(tool)

            diff_decision = kernel.request_tool(step_execution_id, "git", "diff", {"repo_path": str(repo_dir)})
            self.assertTrue(diff_decision.allowed)
            diff_event = next(
                event for event in kernel.event_log.all_events() if event.event_type == "TOOL_INVOKED" and event.payload["action"] == "diff"
            )
            self.assertEqual(diff_event.payload["result"]["status"], "ok")
            self.assertIn("hello git update", diff_event.payload["result"]["data"]["stdout"])

            commit_decision = kernel.request_tool(
                step_execution_id,
                "git",
                "commit",
                {
                    "repo_path": str(repo_dir),
                    "message": "update notes",
                    "files": ["notes.txt"],
                },
            )
            self.assertTrue(commit_decision.allowed)
            commit_event = next(
                event for event in kernel.event_log.all_events() if event.event_type == "TOOL_INVOKED" and event.payload["action"] == "commit"
            )
            self.assertEqual(commit_event.payload["result"]["status"], "ok")
            self.assertIn("commit_hash", commit_event.payload["result"]["data"])

            checkout_decision = kernel.request_tool(
                step_execution_id,
                "git",
                "checkout",
                {"repo_path": str(repo_dir), "branch": "feature/demo"},
            )
            self.assertTrue(checkout_decision.allowed)
            checkout_event = next(
                event for event in kernel.event_log.all_events() if event.event_type == "TOOL_INVOKED" and event.payload["action"] == "checkout"
            )
            self.assertEqual(checkout_event.payload["result"]["status"], "ok")
            self.assertIn("feature/demo", checkout_event.payload["result"]["data"]["stdout"])
            kernel.shutdown()

    def test_git_tool_events_persist_across_runtime_recovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            kernel = RuntimeKernel(event_db, artifact_db)
            repo_dir = Path(temp_dir) / "repo"
            repo_dir.mkdir()
            subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Hermes Bot"], cwd=repo_dir, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "hermes@example.com"], cwd=repo_dir, check=True, capture_output=True, text=True)
            (repo_dir / "notes.txt").write_text("hello git", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True, capture_output=True, text=True)

            step = StepDefinition(
                id="git-4",
                role="developer",
                objective={"description": "Inspect repository status", "required_capabilities": ["code", "reasoning"], "tool_support": True},
                depends_on=[],
                outputs=[],
                constraints={"allowed_tools": ["git"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="git_recovery_workflow",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )
            kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)
            step_execution_id = kernel.schedule_next_step(workflow_execution_id)
            kernel.assign_execution(
                step_execution_id,
                worker_assigned={"worker_type": step.role, "worker_id": "local-worker-1"},
                model_assigned={"adapter": "local", "model_name": "stub-model"},
            )
            kernel.start_execution(step_execution_id)
            tool = Tool(
                tool_id="git",
                name="Git Tool",
                description="Inspect git repositories",
                actions=["status", "diff", "commit", "checkout", "inspect"],
                allowed_roles=["developer"],
                metadata={"kind": "git", "allowed_actions": ["status", "diff", "commit", "checkout", "inspect"]},
            )
            kernel.register_tool(tool)
            decision = kernel.request_tool(step_execution_id, "git", "status", {"repo_path": str(repo_dir)})
            self.assertTrue(decision.allowed)
            kernel.shutdown()

            recovered_kernel = RuntimeKernel(event_db, artifact_db)
            invoke_events = [event for event in recovered_kernel.event_log.all_events() if event.event_type == "TOOL_INVOKED"]
            self.assertEqual(len(invoke_events), 1)
            self.assertEqual(invoke_events[0].payload["tool_id"], "git")
            self.assertEqual(invoke_events[0].payload["action"], "status")
            self.assertEqual(invoke_events[0].payload["result"]["status"], "ok")
            recovered_kernel.shutdown()

    def test_capability_registry_can_select_git_tool_from_capability_metadata(self):
        registry = CapabilityRegistry()
        registry.register_tool(
            "git",
            {
                "kind": "git",
                "tool_id": "git",
                "capabilities": ["code", "reasoning", "tool_usage"],
                "tool_support": True,
                "allowed_actions": ["status", "diff", "commit", "checkout", "inspect"],
            },
        )
        request = CapabilityRequest(
            role="developer",
            required_capabilities=["code", "reasoning"],
            preferred_context_window=65536,
            tool_support=True,
            priority="high",
        )

        selection = registry.resolve_tool(request)
        self.assertTrue(selection["compatible"])
        self.assertEqual(selection["tool_id"], "git")

    def test_capability_resolution_assigns_worker_and_model_from_step_capability(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            kernel = RuntimeKernel(event_db, artifact_db)

            step = StepDefinition(
                id="capability-1",
                role="developer",
                objective={"description": "Implement feature", "required_capabilities": ["code", "reasoning"]},
                depends_on=[],
                outputs=[],
                constraints={"allowed_tools": ["filesystem"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="capability_resolution_workflow",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )
            kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)
            step_execution_id = kernel.schedule_next_step(workflow_execution_id)

            decision = kernel.assign_execution(step_execution_id, model_adapter=StubModelAdapter())

            self.assertTrue(decision.allowed)
            assigned_event = next(event for event in kernel.event_log.all_events() if event.event_type == "STEP_EXECUTION_ASSIGNED")
            self.assertEqual(assigned_event.payload["worker_assigned"]["worker_type"], "developer")
            self.assertEqual(assigned_event.payload["model_assigned"]["capability"], "developer")
            self.assertTrue(assigned_event.payload["model_assigned"]["compatible"])
            self.assertEqual(assigned_event.payload["model_assigned"]["model_name"], "stub-model")
            kernel.shutdown()

    def test_capability_registry_resolves_workers_and_models_deterministically(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            kernel = RuntimeKernel(event_db, artifact_db)

            request = CapabilityRequest(
                role="developer",
                required_capabilities=["code", "reasoning"],
                preferred_context_window=65536,
                tool_support=True,
                priority="high",
            )
            kernel.capability_registry.register_worker(
                "local-developer",
                {
                    "role": "developer",
                    "worker_type": "developer",
                    "capabilities": ["code", "reasoning"],
                    "tool_support": True,
                    "context_window": 65536,
                    "privacy_class": "high",
                },
            )
            kernel.capability_registry.register_worker(
                "legacy-developer",
                {
                    "role": "developer",
                    "worker_type": "developer",
                    "capabilities": ["code"],
                    "tool_support": False,
                    "context_window": 16384,
                    "privacy_class": "low",
                },
            )
            kernel.capability_registry.register_model(
                "stub-model",
                {
                    "name": "stub-model",
                    "provider": "stub",
                    "capabilities": {"code": True, "reasoning": True, "tool_use": True},
                    "context_window": 65536,
                    "latency_class": "low",
                    "cost_class": "low",
                    "privacy_class": "high",
                },
            )

            first_worker = kernel.capability_registry.resolve_worker(request)
            second_worker = kernel.capability_registry.resolve_worker(request)
            model_assignment = kernel.capability_registry.resolve_model(request)

            self.assertTrue(first_worker["compatible"])
            self.assertEqual(first_worker, second_worker)
            self.assertEqual(first_worker["worker_id"], "local-developer")
            self.assertTrue(model_assignment["compatible"])
            self.assertEqual(model_assignment["model_name"], "stub-model")
            self.assertEqual(model_assignment["provider"], "stub")
            kernel.shutdown()

    def test_capability_registry_rejects_incompatible_workers_and_models(self):
        registry = CapabilityRegistry()
        request = CapabilityRequest(
            role="developer",
            required_capabilities=["code", "reasoning"],
            preferred_context_window=65536,
            tool_support=True,
            priority="high",
        )
        registry.register_worker(
            "incompatible-worker",
            {
                "role": "developer",
                "worker_type": "developer",
                "capabilities": ["code"],
                "tool_support": False,
                "context_window": 8192,
                "privacy_class": "low",
            },
        )
        registry.register_model(
            "incompatible-model",
            {
                "name": "incompatible-model",
                "provider": "stub",
                "capabilities": {"code": True},
                "context_window": 8192,
                "latency_class": "low",
                "cost_class": "low",
                "privacy_class": "low",
            },
        )

        worker_assignment = registry.resolve_worker(request)
        model_assignment = registry.resolve_model(request)

        self.assertFalse(worker_assignment["compatible"])
        self.assertEqual(worker_assignment["reason"], "no_compatible_worker")
        self.assertFalse(model_assignment["compatible"])
        self.assertEqual(model_assignment["reason"], ["no_compatible_model"])

    def test_require_approval_transition_moves_workflow_to_waiting_approval(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            runner = RuntimeRunner(event_db, artifact_db)

            step1 = StepDefinition(
                id="architect-1",
                role="architect",
                objective={"description": "Design architecture"},
                depends_on=[],
                outputs=["approval_artifact"],
                constraints={},
            )
            step2 = StepDefinition(
                id="human-approval",
                role="human_operator",
                objective={"description": "Approve architecture"},
                depends_on=["architect-1"],
                outputs=["approval_decision"],
                constraints={},
            )
            workflow_definition = WorkflowDefinition.create(
                name="approval_workflow",
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
            runner.kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = runner.kernel.start_workflow(workflow_definition.workflow_definition_id)
            step_execution_id = runner.kernel.schedule_next_step(workflow_execution_id)
            runner.kernel.assign_execution(
                step_execution_id,
                worker_assigned={"worker_type": "architect", "worker_id": "local-worker-1"},
                model_assigned={"adapter": "local", "model_name": "stub-model"},
            )
            runner.kernel.start_execution(step_execution_id)
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
            runner.kernel.complete_execution(step_execution_id, [artifact])
            self.assertEqual(runner.kernel.get_workflow_status(workflow_execution_id), "WAITING_APPROVAL")
            self.assertIsNone(runner.kernel.schedule_next_step(workflow_execution_id))
            self.assertTrue(any(event.event_type == "APPROVAL_REQUIRED" for event in runner.kernel.event_log.all_events()))
            runner.shutdown()

    def test_recovery_restores_waiting_approval_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            runner = RuntimeRunner(event_db, artifact_db)

            step1 = StepDefinition(
                id="architect-1",
                role="architect",
                objective={"description": "Design architecture"},
                depends_on=[],
                outputs=["approval_artifact"],
                constraints={},
            )
            step2 = StepDefinition(
                id="human-approval",
                role="human_operator",
                objective={"description": "Approve architecture"},
                depends_on=["architect-1"],
                outputs=["approval_decision"],
                constraints={},
            )
            workflow_definition = WorkflowDefinition.create(
                name="approval_recovery_workflow",
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
            runner.kernel.register_workflow_definition(workflow_definition)
            workflow_execution_id = runner.kernel.start_workflow(workflow_definition.workflow_definition_id)
            step_execution_id = runner.kernel.schedule_next_step(workflow_execution_id)
            runner.kernel.assign_execution(
                step_execution_id,
                worker_assigned={"worker_type": "architect", "worker_id": "local-worker-1"},
                model_assigned={"adapter": "local", "model_name": "stub-model"},
            )
            runner.kernel.start_execution(step_execution_id)
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
            runner.kernel.complete_execution(step_execution_id, [artifact])
            runner.kernel.shutdown()

            recovered_kernel = RuntimeKernel(event_db, artifact_db)
            self.assertEqual(recovered_kernel.get_workflow_status(workflow_execution_id), "WAITING_APPROVAL")
            self.assertIsNone(recovered_kernel.schedule_next_step(workflow_execution_id))
            recovered_kernel.shutdown()

    def test_runtime_kernel_can_start_two_executions_from_same_definition(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_db = Path(temp_dir) / "events.db"
            artifact_db = Path(temp_dir) / "artifacts.db"
            runner = RuntimeRunner(event_db, artifact_db)

            step = StepDefinition(
                id="architect-1",
                role="architect",
                objective={"description": "Design the OAuth architecture"},
                depends_on=[],
                outputs=["architecture_v1"],
                constraints={"allowed_tools": ["filesystem"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="build_oauth",
                steps=[step],
                transitions=[],
                policy_refs=[],
            )
            tool = Tool(
                tool_id="filesystem",
                name="Filesystem Tool",
                description="Access files",
                actions=["read"],
                allowed_roles=["architect"],
                metadata={},
            )
            runner.kernel.tool_registry.register(tool)

            with mock.patch("workers.local_worker.LocalWorker.execute") as execute_mock, \
                    mock.patch.object(RuntimeKernel, "request_tool", wraps=runner.kernel.request_tool) as request_tool_mock:
                execute_mock.return_value = WorkerResponse(
                    status="completed",
                    artifacts_created=[Artifact.create(
                        artifact_type="architecture_v1",
                        title="output_for_test",
                        content={"text": "done"},
                        created_by="architect",
                        inputs=[],
                        status="CREATED",
                        decision_record={"reason": "generated"},
                        metadata={},
                    )],
                    observations=[],
                    recommendations=[ToolRequest(tool_id="filesystem", action="read", parameters={"path": "/tmp"})],
                )
                runner.run_workflow(workflow_definition)
                request_tool_mock.assert_called()
            runner.shutdown()
    def test_workflow_approval_state_recovery_matches_original(self):
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
                id="human-approval",
                role="human_operator",
                objective={"description": "Approve architecture"},
                depends_on=["architect-1"],
                outputs=["approval_decision"],
                constraints={},
            )
            workflow_definition = WorkflowDefinition.create(
                name="approval_recovery_workflow",
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
            original_execution = kernel.workflow_executions[workflow_execution_id]
            original_step = kernel.step_executions[step_execution_id]
            kernel.shutdown()

            recovered_kernel = RuntimeKernel(event_db, artifact_db)
            recovered_execution = recovered_kernel.workflow_executions[workflow_execution_id]
            recovered_step = recovered_kernel.step_executions[step_execution_id]

            self.assertEqual(recovered_execution.status, original_execution.status)
            self.assertEqual(recovered_execution.events, original_execution.events)
            self.assertEqual(recovered_execution.completed_executions, original_execution.completed_executions)
            self.assertEqual(recovered_execution.produced_artifacts, original_execution.produced_artifacts)
            self.assertEqual(recovered_step.status, original_step.status)
            self.assertEqual(recovered_step.events, original_step.events)
            self.assertIsNone(recovered_kernel.schedule_next_step(workflow_execution_id))
            self.assertEqual(recovered_kernel.get_workflow_status(workflow_execution_id), "WAITING_APPROVAL")
            recovered_kernel.shutdown()

    def test_failed_execution_recovery_restores_failed_step_state(self):
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
                constraints={"allowed_tools": ["filesystem"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="failure_recovery_workflow",
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
            kernel.fail_execution(step_execution_id, "worker failed", failure_type="worker")
            original_step = kernel.step_executions[step_execution_id]
            original_workflow = kernel.workflow_executions[workflow_execution_id]
            kernel.shutdown()

            recovered_kernel = RuntimeKernel(event_db, artifact_db)
            recovered_step = recovered_kernel.step_executions[step_execution_id]
            recovered_workflow = recovered_kernel.workflow_executions[workflow_execution_id]

            self.assertEqual(recovered_step.status, "FAILED")
            self.assertEqual(recovered_step.completed_at, original_step.completed_at)
            self.assertEqual(recovered_workflow.failed_executions, original_workflow.failed_executions)
            self.assertEqual(recovered_workflow.active_executions, original_workflow.active_executions)
            recovered_kernel.shutdown()

    def test_tool_invocation_events_replay_correctly(self):
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
                constraints={"allowed_tools": ["filesystem"]},
            )
            workflow_definition = WorkflowDefinition.create(
                name="tool_replay_workflow",
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
            tool = Tool(
                tool_id="filesystem",
                name="Filesystem Tool",
                description="Access files",
                actions=["read"],
                allowed_roles=["architect"],
                metadata={},
            )
            kernel.tool_registry.register(tool)
            kernel.request_tool(step_execution_id, "filesystem", "read", {"path": "/tmp"})

            original_step_events = list(kernel.step_executions[step_execution_id].events)
            original_workflow_events = list(kernel.workflow_executions[workflow_execution_id].events)
            kernel.shutdown()

            recovered_kernel = RuntimeKernel(event_db, artifact_db)
            recovered_step_events = recovered_kernel.step_executions[step_execution_id].events
            recovered_workflow_events = recovered_kernel.workflow_executions[workflow_execution_id].events

            self.assertEqual(original_step_events, recovered_step_events)
            self.assertEqual(original_workflow_events, recovered_workflow_events)
            self.assertTrue(any(event.event_type == "TOOL_REQUESTED" for event in recovered_kernel.event_log.all_events()))
            self.assertTrue(any(event.event_type == "TOOL_INVOKED" for event in recovered_kernel.event_log.all_events()))
            recovered_kernel.shutdown()