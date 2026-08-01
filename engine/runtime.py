from __future__ import annotations

import json
import hashlib
from pathlib import Path
import traceback
from typing import Any, Dict, List, Optional

from .models import (
    Artifact,
    CapabilityRequest,
    Event,
    ExecutionContext,
    StepDefinition,
    StepExecution,
    Tool,
    ToolRequest,
    WorkflowDefinition,
    WorkflowExecution,
    WorkerRequest,
    WorkerResponse,
    _now_iso,
)
from .policy import PolicyDecision, PolicyEvaluator
from .storage import SQLiteArtifactStore, SQLiteEventLog, SQLiteToolStore, SQLiteWorkflowDefinitionStore
from .scheduler import Scheduler
from .tools import ToolRegistry
from .capability import CapabilityRegistry, CapabilityResolver
from .metrics import MetricsSink


class RuntimeKernel:
    def __init__(self, event_db_path: Path, artifact_db_path: Path, workflow_definition_db_path: Optional[Path] = None) -> None:
        if workflow_definition_db_path is None:
            workflow_definition_db_path = event_db_path.parent / "workflow_definitions.db"
        self.event_log = SQLiteEventLog(event_db_path)
        self.artifact_store = SQLiteArtifactStore(artifact_db_path)
        self.workflow_definition_store = SQLiteWorkflowDefinitionStore(workflow_definition_db_path)
        self.workflow_definitions: Dict[str, WorkflowDefinition] = {}
        self.workflow_executions: Dict[str, WorkflowExecution] = {}
        self.step_executions: Dict[str, StepExecution] = {}
        self.scheduler = Scheduler(
            self.workflow_definitions,
            self.workflow_executions,
            self.step_executions,
            self.event_log,
            self.artifact_store,
        )
        self.policy_evaluator = PolicyEvaluator()
        self.capability_registry = CapabilityRegistry()
        self.capability_registry.register_default_workers()
        self.metrics: Optional[MetricsSink] = None
        self._load_capability_packages()
        self.tool_registry = ToolRegistry()
        self.tool_store = SQLiteToolStore(event_db_path.parent / "tools.db")
        self._load_definitions()
        self._load_tools()
        self._load_state()

    def _load_capability_packages(self) -> None:
        package_root = Path(__file__).resolve().parents[1] / "capabilities"
        if not package_root.exists():
            return

        for capability_dir in sorted(package_root.iterdir()):
            if not capability_dir.is_dir():
                continue
            capability_file = capability_dir / "capability.json"
            if not capability_file.exists():
                continue
            with capability_file.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            self.capability_registry.register(payload.get("capability") or capability_dir.name, payload)

    def _load_definitions(self) -> None:
        for workflow_definition in self.workflow_definition_store.list():
            self.workflow_definitions[workflow_definition.workflow_definition_id] = workflow_definition

    def _load_tools(self) -> None:
        for tool in self.tool_store.list():
            self.tool_registry.register(tool)

    def _load_state(self) -> None:
        for event in self.event_log.all_events():
            self._apply_event(event, replaying=True)

    def _resolve_workflow_execution_id(self, event: Event) -> Optional[str]:
        payload = event.payload or {}
        if event.event_type in {"WORKFLOW_CREATED", "WORKFLOW_COMPLETED", "WORKFLOW_FAILED", "APPROVAL_REQUIRED", "APPROVAL_GRANTED"}:
            execution_id = payload.get("execution_id")
            if execution_id is not None:
                return execution_id
        if event.execution_id in self.step_executions:
            return self.step_executions[event.execution_id].workflow_id
        workflow_execution_id = payload.get("workflow_execution_id")
        if workflow_execution_id in self.workflow_executions:
            return workflow_execution_id
        return None

    def _reconcile_workflow_state(self, workflow_execution_id: str) -> None:
        workflow_execution = self.workflow_executions.get(workflow_execution_id)
        if workflow_execution is None or workflow_execution.status in {"COMPLETED", "FAILED"}:
            return

        transition_action = self.scheduler.evaluate_workflow_transitions(workflow_execution_id)
        if transition_action is None:
            return

        if transition_action["type"] == "require_approval":
            if workflow_execution.status == "WAITING_APPROVAL":
                return
            if self.approval_already_granted(workflow_execution_id):
                workflow_execution.status = "EXECUTING"
                return
            if any(
                self.event_log.get(event_id) is not None and self.event_log.get(event_id).event_type == "APPROVAL_REQUIRED"
                for event_id in workflow_execution.events
            ):
                return
            self._emit_approval_required(workflow_execution_id, transition_action)

    def _apply_event(self, event: Event, replaying: bool = False) -> None:
        if event.event_type == "WORKFLOW_CREATED":
            payload = event.payload
            workflow_status = payload.get("status", "PLANNING")
            if workflow_status == "CREATED":
                workflow_status = "PLANNING"
            self.workflow_executions[payload["execution_id"]] = WorkflowExecution(
                execution_id=payload["execution_id"],
                workflow_id=payload["workflow_id"],
                workflow_definition_id=payload["workflow_definition_id"],
                workflow_definition_version=payload.get("workflow_definition_version", 1),
                workflow_definition_hash=payload.get("workflow_definition_hash", ""),
                status=workflow_status,
                started_at=payload["started_at"],
                completed_at=payload.get("completed_at"),
                active_executions=[],
                completed_executions=[],
                failed_executions=[],
                produced_artifacts=[],
                events=[event.event_id],
                policy_context=payload.get("policy_context", {"approved": True, "policy_ids": []}),
                execution_context=ExecutionContext.from_dict(payload.get("execution_context")),
            )
        elif event.event_type == "STEP_EXECUTION_CREATED":
            payload = event.payload
            step_execution = StepExecution(
                execution_id=payload["execution_id"],
                workflow_id=payload["workflow_execution_id"],
                workflow_definition_id=payload["workflow_definition_id"],
                step_id=payload["step_id"],
                capability_required=payload["capability_required"],
                status=payload["status"],
                worker_assigned=None,
                model_assigned=None,
                input_artifacts=payload.get("input_artifacts", []),
                output_artifacts=[],
                created_at=payload["created_at"],
                started_at=None,
                completed_at=None,
                events=[event.event_id],
                policy_context=payload.get("policy_context", {"approved": True, "policy_ids": []}),
            )
            self.step_executions[step_execution.execution_id] = step_execution
            workflow_execution = self.workflow_executions[step_execution.workflow_id]
            workflow_execution.active_executions.append(step_execution.execution_id)
            workflow_execution.events.append(event.event_id)
        elif event.event_type in {"STEP_EXECUTION_STARTED", "WORKER_STARTED"}:
            payload = event.payload
            execution = self.step_executions[payload["execution_id"]]
            execution.status = "RUNNING"
            execution.started_at = payload["started_at"]
            execution.events.append(event.event_id)
            workflow_execution = self.workflow_executions[execution.workflow_id]
            if workflow_execution.status in {"PLANNING", "CREATED"}:
                workflow_execution.status = "EXECUTING"
            elif workflow_execution.status == "WAITING_APPROVAL":
                workflow_execution.status = "WAITING_APPROVAL"
            workflow_execution.events.append(event.event_id)
        elif event.event_type in {"STEP_EXECUTION_ASSIGNED", "WORKER_ASSIGNED"}:
            payload = event.payload
            execution = self.step_executions[payload["execution_id"]]
            execution.worker_assigned = payload["worker_assigned"]
            execution.model_assigned = payload["model_assigned"]
            execution.events.append(event.event_id)
        elif event.event_type in {"STEP_EXECUTION_COMPLETED", "WORKER_COMPLETED"}:
            payload = event.payload
            execution = self.step_executions[payload["execution_id"]]
            execution.status = "COMPLETED"
            execution.completed_at = payload["completed_at"]
            execution.output_artifacts = payload.get("output_artifacts", [])
            execution.events.append(event.event_id)
            workflow_execution = self.workflow_executions[execution.workflow_id]
            if execution.execution_id in workflow_execution.active_executions:
                workflow_execution.active_executions.remove(execution.execution_id)
            workflow_execution.completed_executions.append(execution.execution_id)
            workflow_execution.produced_artifacts.extend(execution.output_artifacts)
            workflow_execution.events.append(event.event_id)
        elif event.event_type == "STEP_EXECUTION_FAILED":
            payload = event.payload
            execution = self.step_executions[payload["execution_id"]]
            execution.status = "FAILED"
            execution.completed_at = payload["failed_at"]
            execution.events.append(event.event_id)
            workflow_execution = self.workflow_executions[execution.workflow_id]
            if execution.execution_id in workflow_execution.active_executions:
                workflow_execution.active_executions.remove(execution.execution_id)
            workflow_execution.failed_executions.append(execution.execution_id)
            workflow_execution.events.append(event.event_id)
        elif event.event_type in {"STEP_EXECUTION_POLICY_DENIED", "POLICY_DENIED"}:
            payload = event.payload
            execution = self.step_executions[payload["execution_id"]]
            execution.status = "FAILED"
            execution.completed_at = payload["failed_at"]
            execution.events.append(event.event_id)
            workflow_execution = self.workflow_executions[execution.workflow_id]
            if execution.execution_id in workflow_execution.active_executions:
                workflow_execution.active_executions.remove(execution.execution_id)
            workflow_execution.failed_executions.append(execution.execution_id)
            workflow_execution.events.append(event.event_id)
        elif event.event_type == "TOOL_REQUESTED":
            payload = event.payload
            execution = self.step_executions[payload["execution_id"]]
            execution.events.append(event.event_id)
            workflow_execution = self.workflow_executions[execution.workflow_id]
            workflow_execution.events.append(event.event_id)
        elif event.event_type == "TOOL_INVOKED":
            payload = event.payload
            execution = self.step_executions[payload["execution_id"]]
            execution.events.append(event.event_id)
            workflow_execution = self.workflow_executions[execution.workflow_id]
            workflow_execution.events.append(event.event_id)
        elif event.event_type == "TOOL_FAILED":
            payload = event.payload
            execution = self.step_executions[payload["execution_id"]]
            execution.events.append(event.event_id)
            workflow_execution = self.workflow_executions[execution.workflow_id]
            if execution.execution_id in workflow_execution.active_executions:
                workflow_execution.active_executions.remove(execution.execution_id)
            workflow_execution.failed_executions.append(execution.execution_id)
            workflow_execution.events.append(event.event_id)
        elif event.event_type == "ARTIFACT_CREATED":
            payload = event.payload
            artifact = Artifact(
                artifact_id=payload["artifact_id"],
                artifact_type=payload["artifact_type"],
                version=payload["version"],
                title=payload["title"],
                content=payload["content"],
                content_hash=payload["content_hash"],
                artifact_hash=payload["artifact_hash"],
                created_by=payload["created_by"],
                created_at=payload["created_at"],
                inputs=payload["inputs"],
                parent_version=payload.get("parent_version"),
                status=payload["status"],
                decision_record=payload["decision_record"],
                metadata=payload["metadata"],
            )
            if self.artifact_store.get(artifact.artifact_id, artifact.version) is None:
                self.artifact_store.add(artifact)
            workflow_execution = self.workflow_executions[event.workflow_id]
            workflow_execution.events.append(event.event_id)
        elif event.event_type == "WORKFLOW_COMPLETED":
            payload = event.payload
            workflow_execution = self.workflow_executions[payload["execution_id"]]
            workflow_execution.status = "COMPLETED"
            workflow_execution.completed_at = payload["completed_at"]
            workflow_execution.events.append(event.event_id)
        elif event.event_type in {"APPROVAL_REQUIRED", "TOOL_APPROVAL_REQUIRED"}:
            payload = event.payload
            workflow_execution_id = payload.get("workflow_execution_id") or payload.get("execution_id")
            if workflow_execution_id is None and event.execution_id in self.step_executions:
                workflow_execution_id = self.step_executions[event.execution_id].workflow_id
            if workflow_execution_id is None:
                raise KeyError(f"Unable to resolve workflow execution for approval event {event.event_id}")
            workflow_execution = self.workflow_executions[workflow_execution_id]
            workflow_execution.status = "WAITING_APPROVAL"
            workflow_execution.events.append(event.event_id)
        elif event.event_type in {"APPROVAL_GRANTED", "TOOL_APPROVAL_GRANTED"}:
            payload = event.payload
            workflow_execution_id = payload.get("workflow_execution_id") or payload.get("execution_id")
            if workflow_execution_id is None and event.execution_id in self.step_executions:
                workflow_execution_id = self.step_executions[event.execution_id].workflow_id
            if workflow_execution_id is None:
                raise KeyError(f"Unable to resolve workflow execution for approval granted event {event.event_id}")
            workflow_execution = self.workflow_executions[workflow_execution_id]
            if workflow_execution.status == "WAITING_APPROVAL":
                workflow_execution.status = "EXECUTING"
            workflow_execution.events.append(event.event_id)
        elif event.event_type == "TOOL_APPROVAL_DENIED":
            payload = event.payload
            workflow_execution_id = payload.get("workflow_execution_id") or payload.get("execution_id")
            execution = self.step_executions.get(payload.get("step_execution_id") or event.execution_id)
            if workflow_execution_id is None and execution is not None:
                workflow_execution_id = execution.workflow_id
            if workflow_execution_id is None:
                raise KeyError(f"Unable to resolve workflow execution for tool approval denied event {event.event_id}")
            workflow_execution = self.workflow_executions[workflow_execution_id]
            if execution is not None:
                execution.status = "FAILED"
                execution.completed_at = payload.get("denied_at")
                if execution.execution_id in workflow_execution.active_executions:
                    workflow_execution.active_executions.remove(execution.execution_id)
                if execution.execution_id not in workflow_execution.failed_executions:
                    workflow_execution.failed_executions.append(execution.execution_id)
                execution.events.append(event.event_id)
            workflow_execution.status = "FAILED"
            workflow_execution.completed_at = payload.get("denied_at")
            workflow_execution.events.append(event.event_id)
        elif event.event_type == "WORKFLOW_FAILED":
            payload = event.payload
            workflow_execution = self.workflow_executions[payload["execution_id"]]
            workflow_execution.status = "FAILED"
            workflow_execution.completed_at = payload["completed_at"]
            workflow_execution.events.append(event.event_id)

        if not replaying:
            workflow_execution_id = self._resolve_workflow_execution_id(event)
            if workflow_execution_id is not None:
                self._reconcile_workflow_state(workflow_execution_id)

    def register_workflow_definition(self, workflow_definition: WorkflowDefinition) -> None:
        existing = self.workflow_definition_store.get(workflow_definition.workflow_definition_id)
        if existing is not None:
            if existing.compute_definition_hash() != workflow_definition.compute_definition_hash():
                raise ValueError(
                    f"Immutable workflow definition conflict for id {workflow_definition.workflow_definition_id}"
                )
            return
        self.workflow_definition_store.add(workflow_definition)
        self.workflow_definitions[workflow_definition.workflow_definition_id] = workflow_definition

    def register_tool(self, tool: Tool) -> None:
        existing = self.tool_store.get(tool.tool_id)
        if existing is not None:
            if existing.compute_tool_hash() != tool.compute_tool_hash():
                raise ValueError(f"Immutable tool conflict for id {tool.tool_id}")
            return
        self.tool_store.add(tool)
        self.tool_registry.register(tool)
        self.capability_registry.register_tool(tool.tool_id, tool.to_dict())

    def start_workflow(
        self,
        workflow_definition_id: str,
        execution_context: Optional[ExecutionContext] = None,
        policy_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        workflow_definition = self.workflow_definitions[workflow_definition_id]
        execution_context = execution_context or ExecutionContext.default()
        workflow_execution = WorkflowExecution.create(
            workflow_id=workflow_definition.workflow_id,
            workflow_definition_id=workflow_definition.workflow_definition_id,
            definition_version=workflow_definition.definition_version,
            definition_hash=workflow_definition.compute_definition_hash(),
            execution_context=execution_context,
            policy_context=policy_context,
        )
        event = Event.create(
            event_type="WORKFLOW_CREATED",
            workflow_id=workflow_definition.workflow_id,
            execution_id=workflow_execution.execution_id,
            payload={
                "execution_id": workflow_execution.execution_id,
                "workflow_id": workflow_definition.workflow_id,
                "workflow_definition_id": workflow_definition.workflow_definition_id,
                "workflow_definition_version": workflow_execution.workflow_definition_version,
                "workflow_definition_hash": workflow_execution.workflow_definition_hash,
                "status": workflow_execution.status,
                "started_at": workflow_execution.started_at,
                "policy_context": workflow_execution.policy_context,
                "execution_context": workflow_execution.execution_context.to_dict(),
            },
        )
        self.event_log.append(event)
        self._apply_event(event)
        return workflow_execution.execution_id

    def schedule_next_step(self, workflow_execution_id: str) -> Optional[str]:
        workflow_execution = self.workflow_executions[workflow_execution_id]
        if workflow_execution.status == "WAITING_APPROVAL":
            return None

        transition_action = self.scheduler.evaluate_workflow_transitions(workflow_execution_id)
        if transition_action is not None:
            if transition_action["type"] == "require_approval":
                if self.approval_already_granted(workflow_execution_id):
                    # Approval has already been granted; continue to the next ready step.
                    pass
                else:
                    self._emit_approval_required(workflow_execution_id, transition_action)
                    return None
            if transition_action["type"] == "no_action":
                return None

        next_step = self.scheduler.next_ready_step(workflow_execution_id)
        if next_step is None:
            return None
        return self.create_step_execution(workflow_execution_id, next_step)

    def workflow_complete(self, workflow_execution_id: str) -> bool:
        return self.scheduler.workflow_done(workflow_execution_id)

    def get_workflow_status(self, workflow_execution_id: str) -> str:
        return self.workflow_executions[workflow_execution_id].status

    def approve_workflow(self, workflow_execution_id: str, approved_by: str, reason: str = "approved", comment: Optional[str] = None) -> None:
        workflow_execution = self.workflow_executions[workflow_execution_id]
        if workflow_execution.status != "WAITING_APPROVAL":
            raise ValueError(f"Workflow execution '{workflow_execution_id}' is not waiting for approval")
        approval_event_id = None
        for event_id in reversed(workflow_execution.events):
            event = self.event_log.get(event_id)
            if event is not None and event.event_type == "APPROVAL_REQUIRED":
                approval_event_id = event.event_id
                break
        if approval_event_id is None:
            raise ValueError("No pending approval event found for this workflow")
        event = Event.create(
            event_type="APPROVAL_GRANTED",
            workflow_id=workflow_execution.workflow_id,
            execution_id=workflow_execution.execution_id,
            payload={
                "execution_id": workflow_execution.execution_id,
                "workflow_id": workflow_execution.workflow_id,
                "workflow_definition_id": workflow_execution.workflow_definition_id,
                "approved_by": approved_by,
                "reason": reason,
                "comment": comment,
                "approval_required_event_id": approval_event_id,
            },
        )
        self.event_log.append(event)
        self._apply_event(event)

    def _emit_approval_required(self, workflow_execution_id: str, transition_action: Dict[str, Any]) -> None:
        workflow_execution = self.workflow_executions[workflow_execution_id]
        event = Event.create(
            event_type="APPROVAL_REQUIRED",
            workflow_id=workflow_execution.workflow_id,
            execution_id=workflow_execution.execution_id,
            payload={
                "execution_id": workflow_execution.execution_id,
                "workflow_id": workflow_execution.workflow_id,
                "workflow_definition_id": workflow_execution.workflow_definition_id,
                "role": transition_action.get("role"),
                "reason": "workflow.transition.require_approval",
            },
        )
        event.payload["approval_required_event_id"] = event.event_id
        self.event_log.append(event)
        self._apply_event(event)

    def create_step_execution(self, workflow_execution_id: str, step_definition: StepDefinition) -> str:
        workflow_execution = self.workflow_executions[workflow_execution_id]
        execution = StepExecution.create(
            workflow_id=workflow_execution.execution_id,
            workflow_definition_id=workflow_execution.workflow_definition_id,
            step_id=step_definition.id,
            capability_required=step_definition.role,
            input_artifacts=[],
        )
        event = Event.create(
            event_type="STEP_EXECUTION_CREATED",
            workflow_id=workflow_execution.workflow_id,
            execution_id=execution.execution_id,
            payload={
                "execution_id": execution.execution_id,
                "workflow_execution_id": workflow_execution.execution_id,
                "workflow_id": execution.workflow_id,
                "workflow_definition_id": execution.workflow_definition_id,
                "step_id": execution.step_id,
                "capability_required": execution.capability_required,
                "status": execution.status,
                "created_at": execution.created_at,
                "policy_context": workflow_execution.policy_context,
            },
        )
        self.event_log.append(event)
        self._apply_event(event)
        return execution.execution_id

    def _get_step_definition(self, execution_id: str) -> StepDefinition:
        execution = self.step_executions[execution_id]
        workflow_definition = self.workflow_definitions[execution.workflow_definition_id]
        for step in workflow_definition.steps:
            if step.id == execution.step_id:
                return step
        raise ValueError(f"Step definition not found for step {execution.step_id}")

    def _validate_step_outputs(self, execution_id: str, artifacts: List[Artifact]) -> None:
        step_definition = self._get_step_definition(execution_id)
        expected_outputs = step_definition.outputs or []
        if not expected_outputs:
            return
        if len(artifacts) != len(expected_outputs):
            raise ValueError(
                f"Step '{step_definition.id}' expected {len(expected_outputs)} outputs but received {len(artifacts)}"
            )

        for expected in expected_outputs:
            if not any(
                expected == artifact.artifact_id
                or expected == artifact.artifact_type
                or expected == artifact.title
                for artifact in artifacts
            ):
                raise ValueError(
                    f"Step '{step_definition.id}' expected output '{expected}' was not produced by worker artifacts"
                )

    def fail_execution(self, execution_id: str, reason: str, failure_type: str = "worker") -> None:
        execution = self.step_executions[execution_id]
        event = Event.create(
            event_type="STEP_EXECUTION_FAILED",
            workflow_id=execution.workflow_id,
            execution_id=execution.execution_id,
            payload={
                "execution_id": execution.execution_id,
                "failed_at": _now_iso(),
                "failure_type": failure_type,
                "reason": reason,
            },
        )
        self.event_log.append(event)
        self._apply_event(event)

    def approval_already_granted(self, workflow_execution_id: str) -> bool:
        workflow_execution = self.workflow_executions[workflow_execution_id]

        required_ids = set()

        for event_id in workflow_execution.events:
            event = self.event_log.get(event_id)
            if event and event.event_type == "APPROVAL_REQUIRED":
                required_ids.add(event.event_id)

        for event_id in workflow_execution.events:
            event = self.event_log.get(event_id)
            if (
                event
                and event.event_type == "APPROVAL_GRANTED"
                and event.payload.get("approval_required_event_id") in required_ids
            ):
                return True

        return False

    def _evaluate_execution_policy(self, execution_id: str, action: str, extra_context: Optional[Dict[str, Any]] = None) -> PolicyDecision:
        execution = self.step_executions[execution_id]
        workflow_execution = self.workflow_executions[execution.workflow_id]
        workflow_definition = self.workflow_definitions[execution.workflow_definition_id]
        step_definition = self._get_step_definition(execution_id)
        context = {
            "action": action,
            "execution": {
                "execution_id": execution.execution_id,
                "workflow_id": execution.workflow_id,
                "step_id": execution.step_id,
                "status": execution.status,
            },
            "workflow_execution": {
                "execution_id": workflow_execution.execution_id,
                "workflow_id": workflow_execution.workflow_id,
                "workflow_definition_id": workflow_execution.workflow_definition_id,
                "status": workflow_execution.status,
            },
            "workflow_definition": workflow_definition.to_dict(),
            "step_definition": step_definition.to_dict(),
            "policy_context": execution.policy_context,
            "step_constraints": step_definition.constraints,
        }
        if extra_context is not None:
            context.update(extra_context)
        decision = self.policy_evaluator.evaluate(action, context)
        if not decision.allowed and action != "invoke_tool":
            self._emit_policy_failure(execution_id, decision.reason or "policy.denied")
        return decision

    def _emit_policy_failure(self, execution_id: str, reason: str) -> None:
        execution = self.step_executions[execution_id]
        event = Event.create(
            event_type="STEP_EXECUTION_POLICY_DENIED",
            workflow_id=execution.workflow_id,
            execution_id=execution.execution_id,
            payload={
                "execution_id": execution.execution_id,
                "failed_at": _now_iso(),
                "failure_type": "policy",
                "reason": reason,
            },
        )
        self.event_log.append(event)
        self._apply_event(event)

    def _emit_tool_failed(self, execution_id: str, tool_id: str, action: str, reason: str) -> None:
        execution = self.step_executions[execution_id]
        event = Event.create(
            event_type="TOOL_FAILED",
            workflow_id=execution.workflow_id,
            execution_id=execution.execution_id,
            payload={
                "execution_id": execution.execution_id,
                "step_execution_id": execution.execution_id,
                "tool_id": tool_id,
                "action": action,
                "failed_at": _now_iso(),
                "reason": reason,
            },
        )
        self.event_log.append(event)
        self._apply_event(event)

    @staticmethod
    def _tool_request_fingerprint(tool_id: str, action: str, parameters: Dict[str, Any]) -> str:
        payload = json.dumps(
            {"tool_id": tool_id, "action": action, "parameters": parameters},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _tool_approval_state(self, workflow_execution: WorkflowExecution, approval_id: str, fingerprint: str) -> str:
        state = "none"
        for event_id in workflow_execution.events:
            event = self.event_log.get(event_id)
            if event is None:
                continue
            payload = event.payload or {}
            if payload.get("approval_id") != approval_id or payload.get("request_fingerprint") != fingerprint:
                continue
            if event.event_type == "TOOL_APPROVAL_REQUIRED":
                state = "pending"
            elif event.event_type == "TOOL_APPROVAL_GRANTED":
                state = "granted"
            elif event.event_type == "TOOL_APPROVAL_DENIED":
                state = "denied"
        return state

    def _emit_tool_approval_required(
        self,
        execution_id: str,
        tool_id: str,
        action: str,
        parameters: Dict[str, Any],
        risk_level: str,
        approval_id: str,
        request_fingerprint: str,
    ) -> None:
        execution = self.step_executions[execution_id]
        event = Event.create(
            event_type="TOOL_APPROVAL_REQUIRED",
            workflow_id=execution.workflow_id,
            execution_id=execution.execution_id,
            payload={
                "execution_id": execution.execution_id,
                "workflow_execution_id": execution.workflow_id,
                "step_execution_id": execution.execution_id,
                "approval_id": approval_id,
                "request_fingerprint": request_fingerprint,
                "tool_id": tool_id,
                "action": action,
                "parameters": parameters,
                "risk_level": risk_level,
                "requested_at": _now_iso(),
                "reason": "tool.risk_requires_approval",
            },
        )
        self.event_log.append(event)
        self._apply_event(event)

    def assign_execution(
        self,
        execution_id: str,
        worker_assigned: Optional[Dict[str, Any]] = None,
        model_assigned: Optional[Dict[str, Any]] = None,
        *,
        model_adapter: Optional[Any] = None,
        capability: Optional[str] = None,
        objective: Optional[Dict[str, Any]] = None,
    ) -> PolicyDecision:
        decision = self._evaluate_execution_policy(execution_id, "assign_step")
        if not decision.allowed:
            return decision

        execution = self.step_executions[execution_id]
        step_definition = self._get_step_definition(execution_id)
        resolved_capability = capability or execution.capability_required or step_definition.role
        resolved_objective = objective if objective is not None else step_definition.objective

        execution_context_payload = None
        if workflow_execution := self.workflow_executions.get(execution.workflow_id):
            execution_context_payload = workflow_execution.execution_context.to_dict() if workflow_execution.execution_context else None

        request = CapabilityRequest.from_dict({
            "role": resolved_capability,
            **(resolved_objective or {}),
        })
        if model_adapter is not None:
            self.capability_registry.register_model_adapter(model_adapter)

        required_worker = self.capability_registry.resolve_worker(request, execution_context=execution_context_payload)
        if worker_assigned is None:
            worker_assigned = {
                "worker_type": required_worker["worker_type"],
                "worker_id": required_worker.get("worker_id"),
                "capability": required_worker["role"],
                "profile": {
                    "role": required_worker["role"],
                    "capabilities": required_worker.get("capabilities", []),
                    "tool_support": required_worker.get("tool_support", False),
                    "privacy_class": required_worker.get("privacy_class"),
                },
                "compatible": required_worker["compatible"],
            }
            if not required_worker["compatible"]:
                self._emit_policy_failure(execution_id, "capability.no_compatible_worker")
                return PolicyDecision(False, "capability.no_compatible_worker")

        if model_assigned is None:
            resolved_model = self.capability_registry.resolve_model(request, execution_context=execution_context_payload)
            if model_adapter is not None and not resolved_model["compatible"]:
                self._emit_policy_failure(execution_id, "capability.no_compatible_model")
                return PolicyDecision(False, "capability.no_compatible_model")
            model_assigned = {
                "capability": resolved_model["capability"],
                "provider": resolved_model["provider"],
                "model_name": resolved_model["model_name"],
                "compatible": resolved_model["compatible"],
                "reason": resolved_model["reason"],
                "profile": resolved_model["profile"],
            }

        if execution_context_payload:
            worker_assigned.setdefault("profile", {})["execution_context"] = dict(execution_context_payload)
            model_assigned.setdefault("profile", {})["execution_context"] = dict(execution_context_payload)

        if model_adapter is not None and not model_assigned.get("compatible", True):
            self._emit_policy_failure(execution_id, "capability.model_not_compatible")
            return PolicyDecision(False, "capability.model_not_compatible")

        event = Event.create(
            event_type="STEP_EXECUTION_ASSIGNED",
            workflow_id=execution.workflow_id,
            execution_id=execution.execution_id,
            payload={
                "execution_id": execution.execution_id,
                "worker_assigned": worker_assigned,
                "model_assigned": model_assigned,
            },
        )
        self.event_log.append(event)
        self._apply_event(event)
        return decision

    def request_tool(self, execution_id: str, tool_id: str, action: str, parameters: Dict[str, Any]) -> PolicyDecision:
        execution = self.step_executions[execution_id]
        workflow_execution = self.workflow_executions[execution.workflow_id]
        step_definition = self._get_step_definition(execution_id)
        tool = self.tool_registry.get(tool_id)
        if tool is None:
            self._emit_tool_failed(execution_id, tool_id, action, "tool_registry.tool_not_found")
            return PolicyDecision(False, "tool_registry.tool_not_found")

        tool_metadata = dict(tool.metadata or {})
        execution_context = workflow_execution.execution_context or ExecutionContext.default()
        capability_contract = execution_context.capability_contract or {}
        if isinstance(capability_contract, dict) and capability_contract:
            tool_metadata["capability_contract"] = dict(capability_contract)
            if tool_id == "filesystem":
                tool_metadata.setdefault("allowed_roots", list(capability_contract.get("allowed_roots") or []))
            if tool_id == "shell":
                tool_metadata.setdefault("allowed_cwds", [str(root) for root in capability_contract.get("allowed_roots") or []])
                tool_metadata.setdefault("allowed_env_keys", list(capability_contract.get("allowed_env_keys") or []))

        tool_constraints = {
            "command": parameters.get("command"),
            "allowed_commands": tool_metadata.get("allowed_commands", []),
            "allowed_actions": tool_metadata.get("allowed_actions", []),
            "cwd": parameters.get("cwd"),
            "env": parameters.get("env"),
            "path": parameters.get("path"),
            "source": parameters.get("source"),
            "destination": parameters.get("destination"),
        }
        tool_capabilities = []
        if tool_id == "filesystem":
            tool_capabilities = ["workspace_inspection", "repo_editing"]
        elif tool_id == "shell":
            tool_capabilities = ["workspace_execution"]
        elif tool_id == "git":
            tool_capabilities = ["repo_editing"]

        policy_context = {
            "tool_id": tool_id,
            "tool_action": action,
            "tool_risk_level": tool.risk_level_for_action(action),
            "policy_context": execution.policy_context,
            "tool_constraints": tool_constraints,
            "sandbox_profile": execution_context.sandbox_profile,
            "capability_contract": capability_contract,
            "capability_requirements": [],
            "tool_capabilities": tool_capabilities,
        }
        decision = self.policy_evaluator.evaluate_tool_request(
            tool_id=tool_id,
            tool_action=action,
            policy_context=execution.policy_context,
            step_constraints=step_definition.constraints,
            tool_constraints=tool_constraints,
            extra_context=policy_context,
        )
        if not decision.allowed:
            self._emit_policy_failure(execution_id, decision.reason or "policy.invoke_denied")
            return decision

        if not self.tool_registry.authorize(execution.capability_required, tool_id, action):
            self._emit_tool_failed(execution_id, tool_id, action, "tool_registry.unauthorized")
            return PolicyDecision(False, "tool_registry.unauthorized")

        risk_level = tool.risk_level_for_action(action)
        if risk_level in {"destructive", "privileged", "external_network"}:
            request_fingerprint = self._tool_request_fingerprint(tool_id, action, parameters)
            approval_id = f"tool-approval-{request_fingerprint[:16]}"
            approval_state = self._tool_approval_state(workflow_execution, approval_id, request_fingerprint)
            if approval_state != "granted":
                if approval_state == "none":
                    self._emit_tool_approval_required(
                        execution_id,
                        tool_id,
                        action,
                        parameters,
                        risk_level,
                        approval_id,
                        request_fingerprint,
                    )
                return PolicyDecision(False, "tool_approval_required")

        request_event = Event.create(
            event_type="TOOL_REQUESTED",
            workflow_id=workflow_execution.workflow_id,
            execution_id=execution.execution_id,
            payload={
                "execution_id": execution.execution_id,
                "workflow_id": workflow_execution.workflow_id,
                "step_execution_id": execution.execution_id,
                "tool_id": tool_id,
                "action": action,
                "risk_level": risk_level,
                "parameters": parameters,
                "policy_decision": {
                    "allowed": decision.allowed,
                    "reason": decision.reason,
                },
                "risk_level": risk_level,
            },
        )
        self.event_log.append(request_event)
        self._apply_event(request_event)

        result = None
        try:
            from .tool_runtime import FilesystemTool, GitTool, ShellTool

            tool_runtime = None
            metadata = tool.metadata or {}
            if tool_id == "filesystem":
                tool_runtime = FilesystemTool(allowed_roots=metadata.get("allowed_roots", []), metadata=metadata)
            elif tool_id == "shell":
                tool_runtime = ShellTool(allowed_commands=metadata.get("allowed_commands", []), metadata=metadata)
            elif tool_id == "git":
                tool_runtime = GitTool(allowed_actions=metadata.get("allowed_actions", []), metadata=metadata)
            if tool_runtime is None:
                raise ValueError(f"No runtime implementation for tool '{tool_id}'")
            tool_request = ToolRequest(tool_id=tool_id, action=action, parameters=parameters)
            result = tool_runtime.execute(tool_request)
        except Exception as exc:
            self._emit_tool_failed(execution_id, tool_id, action, str(exc))
            return PolicyDecision(False, str(exc))

        invoke_event = Event.create(
            event_type="TOOL_INVOKED",
            workflow_id=workflow_execution.workflow_id,
            execution_id=execution.execution_id,
            payload={
                "execution_id": execution.execution_id,
                "step_execution_id": execution.execution_id,
                "tool_id": tool_id,
                "action": action,
                "parameters": parameters,
                "policy_decision": {
                    "allowed": decision.allowed,
                    "reason": decision.reason,
                },
                "result": result.to_dict(),
            },
        )
        self.event_log.append(invoke_event)
        self._apply_event(invoke_event)
        return decision

    def approve_tool_request(
        self,
        execution_id: str,
        approval_id: str,
        *,
        approved_by: str,
        reason: str = "approved",
        comment: Optional[str] = None,
    ) -> PolicyDecision:
        execution = self.step_executions[execution_id]
        workflow_execution = self.workflow_executions[execution.workflow_id]
        pending_event = None
        for event_id in reversed(workflow_execution.events):
            event = self.event_log.get(event_id)
            if event is not None and event.event_type == "TOOL_APPROVAL_REQUIRED" and event.payload.get("approval_id") == approval_id:
                pending_event = event
                break
        if pending_event is None:
            return PolicyDecision(False, "tool_approval_not_found")

        request_fingerprint = pending_event.payload.get("request_fingerprint")
        if self._tool_approval_state(workflow_execution, approval_id, request_fingerprint) != "pending":
            return PolicyDecision(False, "tool_approval_not_pending")

        event = Event.create(
            event_type="TOOL_APPROVAL_GRANTED",
            workflow_id=workflow_execution.workflow_id,
            execution_id=workflow_execution.execution_id,
            payload={
                "execution_id": workflow_execution.execution_id,
                "approval_id": approval_id,
                "request_fingerprint": request_fingerprint,
                "tool_id": pending_event.payload.get("tool_id"),
                "action": pending_event.payload.get("action"),
                "risk_level": pending_event.payload.get("risk_level"),
                "approved_by": approved_by,
                "reason": reason,
                "comment": comment,
                "approved_at": _now_iso(),
                "approval_required_event_id": pending_event.event_id,
            },
        )
        self.event_log.append(event)
        self._apply_event(event)
        return PolicyDecision(True, "tool_approval_granted")

    def deny_tool_request(
        self,
        execution_id: str,
        approval_id: str,
        *,
        denied_by: str,
        reason: str = "denied",
        comment: Optional[str] = None,
    ) -> PolicyDecision:
        execution = self.step_executions[execution_id]
        workflow_execution = self.workflow_executions[execution.workflow_id]
        pending_event = None
        for event_id in reversed(workflow_execution.events):
            event = self.event_log.get(event_id)
            if event is not None and event.event_type == "TOOL_APPROVAL_REQUIRED" and event.payload.get("approval_id") == approval_id:
                pending_event = event
                break
        if pending_event is None:
            return PolicyDecision(False, "tool_approval_not_found")

        request_fingerprint = pending_event.payload.get("request_fingerprint")
        if self._tool_approval_state(workflow_execution, approval_id, request_fingerprint) != "pending":
            return PolicyDecision(False, "tool_approval_not_pending")

        event = Event.create(
            event_type="TOOL_APPROVAL_DENIED",
            workflow_id=workflow_execution.workflow_id,
            execution_id=workflow_execution.execution_id,
            payload={
                "execution_id": workflow_execution.execution_id,
                "step_execution_id": execution_id,
                "approval_id": approval_id,
                "request_fingerprint": request_fingerprint,
                "tool_id": pending_event.payload.get("tool_id"),
                "action": pending_event.payload.get("action"),
                "risk_level": pending_event.payload.get("risk_level"),
                "denied_by": denied_by,
                "reason": reason,
                "comment": comment,
                "denied_at": _now_iso(),
                "approval_required_event_id": pending_event.event_id,
            },
        )
        self.event_log.append(event)
        self._apply_event(event)
        return PolicyDecision(False, "tool_approval_denied")

    def start_execution(self, execution_id: str) -> PolicyDecision:
        decision = self._evaluate_execution_policy(execution_id, "start_step")
        if not decision.allowed:
            return decision
        execution = self.step_executions[execution_id]
        event = Event.create(
            event_type="STEP_EXECUTION_STARTED",
            workflow_id=execution.workflow_id,
            execution_id=execution.execution_id,
            payload={
                "execution_id": execution.execution_id,
                "started_at": _now_iso(),
            },
        )
        self.event_log.append(event)
        self._apply_event(event)
        return decision

    def complete_execution(self, execution_id: str, artifacts: List[Artifact]) -> None:
        try:
            self._validate_step_outputs(execution_id, artifacts)
        except ValueError as exc:
            self.fail_execution(execution_id, str(exc), failure_type="validation")
            raise

        execution = self.step_executions[execution_id]
        for artifact in artifacts:
            artifact_event = Event.create(
                event_type="ARTIFACT_CREATED",
                workflow_id=execution.workflow_id,
                execution_id=execution.execution_id,
                payload={
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "version": artifact.version,
                    "title": artifact.title,
                    "content": artifact.content,
                    "content_hash": artifact.content_hash,
                    "artifact_hash": artifact.artifact_hash,
                    "created_by": artifact.created_by,
                    "created_at": artifact.created_at,
                    "inputs": artifact.inputs,
                    "parent_version": artifact.parent_version,
                    "status": artifact.status,
                    "decision_record": artifact.decision_record,
                    "metadata": artifact.metadata,
                },
            )
            self.event_log.append(artifact_event)
            self._apply_event(artifact_event)
        event = Event.create(
            event_type="STEP_EXECUTION_COMPLETED",
            workflow_id=execution.workflow_id,
            execution_id=execution.execution_id,
            payload={
                "execution_id": execution.execution_id,
                "completed_at": _now_iso(),
                "output_artifacts": [artifact.artifact_id for artifact in artifacts],
            },
        )
        self.event_log.append(event)
        self._apply_event(event)


    def complete_workflow(self, workflow_execution_id: str) -> None:
        workflow_execution = self.workflow_executions[workflow_execution_id]
        event = Event.create(
            event_type="WORKFLOW_COMPLETED",
            workflow_id=workflow_execution.workflow_id,
            execution_id=workflow_execution.execution_id,
            payload={
                "execution_id": workflow_execution.execution_id,
                "completed_at": _now_iso(),
            },
        )
        self.event_log.append(event)
        self._apply_event(event)

    def shutdown(self) -> None:
        try:
            if self.metrics is not None:
                self.metrics.close()
        except Exception:
            pass
        self.event_log.close()
        self.artifact_store.close()
        self.tool_store.close()
        self.workflow_definition_store.close()
