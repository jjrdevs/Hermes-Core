from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


@dataclasses.dataclass(frozen=True)
class Event:
    event_id: str
    event_type: str
    timestamp: str
    workflow_id: str
    execution_id: Optional[str]
    payload: Dict[str, Any]

    @staticmethod
    def create(event_type: str, workflow_id: str, payload: Dict[str, Any], execution_id: Optional[str] = None) -> "Event":
        return Event(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            timestamp=_now_iso(),
            workflow_id=workflow_id,
            execution_id=execution_id,
            payload=payload,
        )


@dataclasses.dataclass(frozen=True)
class Artifact:
    artifact_id: str
    artifact_type: str
    version: int
    title: str
    content: Any
    content_hash: str
    artifact_hash: str
    created_by: str
    created_at: str
    inputs: List[str]
    parent_version: Optional[int]
    status: str
    decision_record: Dict[str, Any]
    metadata: Dict[str, Any]

    @staticmethod
    def compute_content_hash(content: Any) -> str:
        serialized = json.dumps(content, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def compute_artifact_hash(record: Dict[str, Any]) -> str:
        serialized = json.dumps(record, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def create(
        artifact_type: str,
        title: str,
        content: Any,
        created_by: str,
        artifact_id: Optional[str] = None,
        inputs: Optional[List[str]] = None,
        parent_version: Optional[int] = None,
        status: str = "CREATED",
        decision_record: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "Artifact":
        inputs = inputs or []
        metadata = metadata or {}
        decision_record = decision_record or {}
        created_at = _now_iso()
        content_hash = Artifact.compute_content_hash(content)
        base_record = {
            "artifact_id": artifact_id or str(uuid.uuid4()),
            "artifact_type": artifact_type,
            "version": 1 if parent_version is None else parent_version + 1,
            "title": title,
            "content": content,
            "content_hash": content_hash,
            "created_by": created_by,
            "created_at": created_at,
            "inputs": inputs,
            "parent_version": parent_version,
            "status": status,
            "decision_record": decision_record,
            "metadata": metadata,
        }
        artifact_hash = Artifact.compute_artifact_hash(base_record)
        return Artifact(
            artifact_id=artifact_id or str(uuid.uuid4()),
            artifact_type=artifact_type,
            version=1 if parent_version is None else parent_version + 1,
            title=title,
            content=content,
            content_hash=content_hash,
            artifact_hash=artifact_hash,
            created_by=created_by,
            created_at=created_at,
            inputs=inputs,
            parent_version=parent_version,
            status=status,
            decision_record=decision_record,
            metadata=metadata,
        )


@dataclasses.dataclass(frozen=True)
class StepDefinition:
    id: str
    role: str
    objective: Dict[str, Any]
    depends_on: List[str]
    outputs: List[str]
    constraints: Dict[str, Any]

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "StepDefinition":
        return StepDefinition(
            id=data["id"],
            role=data["role"],
            objective=data.get("objective", {}),
            depends_on=data.get("depends_on", []),
            outputs=data.get("outputs", []),
            constraints=data.get("constraints", {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "objective": self.objective,
            "depends_on": self.depends_on,
            "outputs": self.outputs,
            "constraints": self.constraints,
        }


@dataclasses.dataclass(frozen=True)
class WorkflowDefinition:
    workflow_definition_id: str
    workflow_id: str
    name: str
    created_at: str
    steps: List[StepDefinition]
    transitions: List[Dict[str, Any]]
    policy_refs: List[str]
    definition_version: int

    @staticmethod
    def create(
        name: str,
        steps: List[StepDefinition],
        transitions: Optional[List[Dict[str, Any]]] = None,
        policy_refs: Optional[List[str]] = None,
        workflow_id: Optional[str] = None,
        workflow_definition_id: Optional[str] = None,
    ) -> "WorkflowDefinition":
        return WorkflowDefinition(
            workflow_definition_id=workflow_definition_id or str(uuid.uuid4()),
            workflow_id=workflow_id or str(uuid.uuid4()),
            name=name,
            created_at=_now_iso(),
            steps=steps,
            transitions=transitions or [],
            policy_refs=policy_refs or [],
            definition_version=1,
        )

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "WorkflowDefinition":
        return WorkflowDefinition(
            workflow_definition_id=data["workflow_definition_id"],
            workflow_id=data["workflow_id"],
            name=data["name"],
            created_at=data.get("created_at", _now_iso()),
            steps=[StepDefinition.from_dict(step) for step in data.get("steps", [])],
            transitions=data.get("transitions", []),
            policy_refs=data.get("policy_refs", []),
            definition_version=data.get("definition_version", 1),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workflow_definition_id": self.workflow_definition_id,
            "workflow_id": self.workflow_id,
            "name": self.name,
            "created_at": self.created_at,
            "steps": [step.to_dict() for step in self.steps],
            "transitions": self.transitions,
            "policy_refs": self.policy_refs,
            "definition_version": self.definition_version,
        }

    def compute_definition_hash(self) -> str:
        serialized = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclasses.dataclass
class StepExecution:
    execution_id: str
    workflow_id: str
    workflow_definition_id: str
    step_id: str
    capability_required: str
    status: str
    worker_assigned: Optional[Dict[str, Any]]
    model_assigned: Optional[Dict[str, Any]]
    input_artifacts: List[str]
    output_artifacts: List[str]
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]
    events: List[str]
    policy_context: Dict[str, Any]

    @staticmethod
    def create(workflow_id: str, workflow_definition_id: str, step_id: str, capability_required: str, input_artifacts: Optional[List[str]] = None) -> "StepExecution":
        return StepExecution(
            execution_id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            workflow_definition_id=workflow_definition_id,
            step_id=step_id,
            capability_required=capability_required,
            status="PENDING",
            worker_assigned=None,
            model_assigned=None,
            input_artifacts=input_artifacts or [],
            output_artifacts=[],
            created_at=_now_iso(),
            started_at=None,
            completed_at=None,
            events=[],
            policy_context={"approved": True, "policy_ids": []},
        )


@dataclasses.dataclass
class WorkflowExecution:
    execution_id: str
    workflow_id: str
    workflow_definition_id: str
    workflow_definition_version: int
    workflow_definition_hash: str
    status: str
    started_at: str
    completed_at: Optional[str]
    active_executions: List[str]
    completed_executions: List[str]
    failed_executions: List[str]
    produced_artifacts: List[str]
    events: List[str]
    policy_context: Dict[str, Any]

    @staticmethod
    def create(workflow_id: str, workflow_definition_id: str, definition_version: int, definition_hash: str) -> "WorkflowExecution":
        return WorkflowExecution(
            execution_id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            workflow_definition_id=workflow_definition_id,
            workflow_definition_version=definition_version,
            workflow_definition_hash=definition_hash,
            status="PLANNING",
            started_at=_now_iso(),
            completed_at=None,
            active_executions=[],
            completed_executions=[],
            failed_executions=[],
            produced_artifacts=[],
            events=[],
            policy_context={"approved": True, "policy_ids": []},
        )


@dataclasses.dataclass(frozen=True)
class WorkerRequest:
    execution_id: str
    workflow_id: str
    role: str
    objective: Dict[str, Any]
    context: Dict[str, Any]
    constraints: Dict[str, Any]


@dataclasses.dataclass(frozen=True)
class Tool:
    tool_id: str
    name: str
    description: str
    actions: List[str]
    allowed_roles: List[str]
    metadata: Dict[str, Any]

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Tool":
        return Tool(
            tool_id=data["tool_id"],
            name=data["name"],
            description=data["description"],
            actions=data.get("actions", []),
            allowed_roles=data.get("allowed_roles", []),
            metadata=data.get("metadata", {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "name": self.name,
            "description": self.description,
            "actions": self.actions,
            "allowed_roles": self.allowed_roles,
            "metadata": self.metadata,
        }

    def compute_tool_hash(self) -> str:
        serialized = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class ToolRequest:
    tool_id: str
    action: str
    parameters: Dict[str, Any]


@dataclasses.dataclass(frozen=True)
class WorkerResponse:
    status: str
    artifacts_created: List[Artifact]
    observations: List[Dict[str, Any]]
    recommendations: List[ToolRequest]
