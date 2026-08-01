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
class CapabilityRequest:
    role: str
    required_capabilities: List[str] = dataclasses.field(default_factory=list)
    preferred_context_window: Optional[int] = None
    tool_support: Optional[bool] = None
    priority: str = "normal"
    latency_class: Optional[str] = None
    cost_class: Optional[str] = None
    privacy_class: Optional[str] = None
    function_support: Optional[bool] = None

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "CapabilityRequest":
        if not data:
            return cls(role="developer")
        return cls(
            role=data.get("role", "developer"),
            required_capabilities=list(data.get("required_capabilities", []) or []),
            preferred_context_window=data.get("preferred_context_window"),
            tool_support=data.get("tool_support"),
            priority=data.get("priority", "normal"),
            latency_class=data.get("latency_class"),
            cost_class=data.get("cost_class"),
            privacy_class=data.get("privacy_class"),
            function_support=data.get("function_support"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "required_capabilities": list(self.required_capabilities),
            "preferred_context_window": self.preferred_context_window,
            "tool_support": self.tool_support,
            "priority": self.priority,
            "latency_class": self.latency_class,
            "cost_class": self.cost_class,
            "privacy_class": self.privacy_class,
            "function_support": self.function_support,
        }


@dataclasses.dataclass(frozen=True)
class ExecutionContext:
    execution_mode: str = "autonomous"
    policy_profile: str = "default"
    sandbox_profile: str = "standard"
    approval_requirements: Dict[str, Any] = dataclasses.field(default_factory=dict)
    resource_constraints: Dict[str, Any] = dataclasses.field(default_factory=dict)
    environment_metadata: Dict[str, Any] = dataclasses.field(default_factory=dict)
    capability_contract: Dict[str, Any] = dataclasses.field(default_factory=dict)

    @staticmethod
    def default() -> "ExecutionContext":
        return ExecutionContext()

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "ExecutionContext":
        if not data:
            return cls.default()
        return cls(
            execution_mode=data.get("execution_mode", "autonomous"),
            policy_profile=data.get("policy_profile", "default"),
            sandbox_profile=data.get("sandbox_profile", "standard"),
            approval_requirements=data.get("approval_requirements", {}),
            resource_constraints=data.get("resource_constraints", {}),
            environment_metadata=data.get("environment_metadata", {}),
            capability_contract=dict(data.get("capability_contract") or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "execution_mode": self.execution_mode,
            "policy_profile": self.policy_profile,
            "sandbox_profile": self.sandbox_profile,
            "approval_requirements": self.approval_requirements,
            "resource_constraints": self.resource_constraints,
            "environment_metadata": self.environment_metadata,
            "capability_contract": dict(self.capability_contract),
        }


@dataclasses.dataclass
class CapabilityContract:
    allowed_commands: List[str] = dataclasses.field(default_factory=list)
    allowed_roots: List[str] = dataclasses.field(default_factory=list)
    allowed_env_keys: List[str] = dataclasses.field(default_factory=list)
    allow_network: bool = True
    writable_paths: List[str] = dataclasses.field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "CapabilityContract":
        if not data:
            return cls()
        return cls(
            allowed_commands=[str(item) for item in list(data.get("allowed_commands") or [])],
            allowed_roots=[str(item) for item in list(data.get("allowed_roots") or [])],
            allowed_env_keys=[str(item) for item in list(data.get("allowed_env_keys") or [])],
            allow_network=bool(data.get("allow_network", True)),
            writable_paths=[str(item) for item in list(data.get("writable_paths") or [])],
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed_commands": list(self.allowed_commands),
            "allowed_roots": list(self.allowed_roots),
            "allowed_env_keys": list(self.allowed_env_keys),
            "allow_network": self.allow_network,
            "writable_paths": list(self.writable_paths),
        }


@dataclasses.dataclass
class RepositoryTaskSpec:
    description: str
    workspace_path: Optional[str] = None
    expected_files: List[str] = dataclasses.field(default_factory=list)
    selected_files: List[str] = dataclasses.field(default_factory=list)
    verification_command: Optional[str] = None
    verification_expectations: Dict[str, Any] = dataclasses.field(default_factory=dict)
    allowed_tools: List[str] = dataclasses.field(default_factory=list)
    constraints: Dict[str, Any] = dataclasses.field(default_factory=dict)
    policy_context: Dict[str, Any] = dataclasses.field(default_factory=dict)
    budget: Dict[str, Any] = dataclasses.field(default_factory=dict)
    stop_reason: Optional[str] = None
    capability_contract: Dict[str, Any] = dataclasses.field(default_factory=dict)
    sandbox_profile: Optional[str] = None
    sandbox_policy: Dict[str, Any] = dataclasses.field(default_factory=dict)
    patch_preview: Dict[str, Any] = dataclasses.field(default_factory=dict)
    plan_artifact: Dict[str, Any] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "RepositoryTaskSpec":
        if not data:
            return cls(description="")
        return cls(
            description=str(data.get("description") or ""),
            workspace_path=data.get("workspace_path"),
            expected_files=list(data.get("expected_files") or []),
            selected_files=list(data.get("selected_files") or []),
            verification_command=data.get("verification_command"),
            verification_expectations=dict(data.get("verification_expectations") or {}),
            allowed_tools=list(data.get("allowed_tools") or []),
            constraints=dict(data.get("constraints") or {}),
            policy_context=dict(data.get("policy_context") or {}),
            budget=dict(data.get("budget") or {}),
            stop_reason=data.get("stop_reason"),
            capability_contract=dict(data.get("capability_contract") or {}),
            sandbox_profile=data.get("sandbox_profile"),
            sandbox_policy=dict(data.get("sandbox_policy") or {}),
            patch_preview=dict(data.get("patch_preview") or {}),
            plan_artifact=dict(data.get("plan_artifact") or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "description": self.description,
            "workspace_path": self.workspace_path,
            "expected_files": list(self.expected_files),
            "selected_files": list(self.selected_files),
            "verification_command": self.verification_command,
            "verification_expectations": dict(self.verification_expectations),
            "allowed_tools": list(self.allowed_tools),
            "constraints": dict(self.constraints),
            "policy_context": dict(self.policy_context),
            "budget": dict(self.budget),
            "stop_reason": self.stop_reason,
            "capability_contract": dict(self.capability_contract),
            "sandbox_profile": self.sandbox_profile,
            "sandbox_policy": dict(self.sandbox_policy),
            "patch_preview": dict(self.patch_preview),
            "plan_artifact": dict(self.plan_artifact),
        }


@dataclasses.dataclass
class TaskEnvelope:
    objective: Dict[str, Any]
    constraints: Dict[str, Any]
    acceptance_criteria: List[str]
    allowed_tools: List[str]
    budget: Dict[str, Any]
    execution_context: Dict[str, Any] = dataclasses.field(default_factory=dict)
    policy_context: Dict[str, Any] = dataclasses.field(default_factory=dict)
    capability_requirements: List[str] = dataclasses.field(default_factory=list)
    task_spec: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "TaskEnvelope":
        if not data:
            return cls(
                objective={},
                constraints={},
                acceptance_criteria=[],
                allowed_tools=[],
                budget={},
            )
        return cls(
            objective=dict(data.get("objective") or {}),
            constraints=dict(data.get("constraints") or {}),
            acceptance_criteria=list(data.get("acceptance_criteria") or []),
            allowed_tools=list(data.get("allowed_tools") or []),
            budget=dict(data.get("budget") or {}),
            execution_context=dict(data.get("execution_context") or {}),
            policy_context=dict(data.get("policy_context") or {}),
            capability_requirements=list(data.get("capability_requirements") or []),
            task_spec=dict(data.get("task_spec") or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "objective": dict(self.objective),
            "constraints": dict(self.constraints),
            "acceptance_criteria": list(self.acceptance_criteria),
            "allowed_tools": list(self.allowed_tools),
            "budget": dict(self.budget),
            "execution_context": dict(self.execution_context),
            "policy_context": dict(self.policy_context),
            "capability_requirements": list(self.capability_requirements),
            "task_spec": dict(self.task_spec or {}),
        }


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
        artifact_id = artifact_id or str(uuid.uuid4())
        version = 1 if parent_version is None else parent_version + 1
        inputs = inputs or []
        metadata = metadata or {}
        decision_record = decision_record or {}
        created_at = _now_iso()
        content_hash = Artifact.compute_content_hash(content)
        base_record = {
            "artifact_id": artifact_id,
            "artifact_type": artifact_type,
            "version": version,
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
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            version=version,
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
    execution_context: Optional[ExecutionContext] = None

    @staticmethod
    def create(
        workflow_id: str,
        workflow_definition_id: str,
        definition_version: int,
        definition_hash: str,
        execution_context: Optional[ExecutionContext] = None,
        policy_context: Optional[Dict[str, Any]] = None,
    ) -> "WorkflowExecution":
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
            policy_context=policy_context or {"approved": True, "policy_ids": []},
            execution_context=execution_context or ExecutionContext.default(),
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
    risk_level: Optional[str] = None

    VALID_RISK_LEVELS = frozenset({"read_only", "write_safe", "destructive", "privileged", "external_network"})

    def risk_level_for_action(self, action: Optional[str] = None) -> str:
        explicit_risk = self.risk_level or (self.metadata or {}).get("risk_level")
        if explicit_risk:
            if explicit_risk not in self.VALID_RISK_LEVELS:
                raise ValueError(f"Unsupported tool risk level: {explicit_risk}")
            return explicit_risk

        action_name = str(action or "").lower()
        action_names = {action_name} if action_name else {str(item).lower() for item in self.actions}
        if self.tool_id in {"web_search", "network", "http"} or (self.metadata or {}).get("network"):
            return "external_network"
        if self.tool_id == "git" and action_names.intersection({"commit", "checkout", "reset", "merge", "push"}):
            return "destructive"
        if action_names.intersection({"delete", "delete_file", "move", "move_file", "remove", "reset"}):
            return "destructive"
        if action_names.intersection({"write", "write_file", "create_file", "create_directory", "copy", "copy_file", "run", "exec"}):
            return "write_safe"
        return "read_only"

    def resolved_risk_level(self) -> str:
        risk_order = {"read_only": 0, "write_safe": 1, "destructive": 2, "privileged": 3, "external_network": 4}
        return max((self.risk_level_for_action(action) for action in self.actions), key=risk_order.get, default="read_only")

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Tool":
        return Tool(
            tool_id=data["tool_id"],
            name=data["name"],
            description=data["description"],
            actions=data.get("actions", []),
            allowed_roles=data.get("allowed_roles", []),
            metadata=data.get("metadata", {}),
            risk_level=data.get("risk_level"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "name": self.name,
            "description": self.description,
            "actions": self.actions,
            "allowed_roles": self.allowed_roles,
            "metadata": self.metadata,
            "risk_level": self.resolved_risk_level(),
        }

    def compute_tool_hash(self) -> str:
        serialized = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True)
class ToolContract:
    tool_id: str
    name: str
    description: str
    actions: List[str]
    allowed_roles: List[str]
    metadata: Dict[str, Any]
    risk_level: str = "low"
    timeout_seconds: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "name": self.name,
            "description": self.description,
            "actions": list(self.actions),
            "allowed_roles": list(self.allowed_roles),
            "metadata": dict(self.metadata),
            "risk_level": self.risk_level,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclasses.dataclass(frozen=True)
class CapabilityContract:
    allowed_commands: Optional[List[str]] = None
    allowed_roots: Optional[List[str]] = None
    allowed_env_keys: Optional[List[str]] = None
    writable_paths: Optional[List[str]] = None
    allow_network: Optional[bool] = None

    @staticmethod
    def from_dict(data: Any) -> "CapabilityContract":
        if not isinstance(data, dict):
            return CapabilityContract()
        return CapabilityContract(
            allowed_commands=[str(item) for item in data.get("allowed_commands") or []],
            allowed_roots=[str(item) for item in data.get("allowed_roots") or []],
            allowed_env_keys=[str(item) for item in data.get("allowed_env_keys") or []],
            writable_paths=[str(item) for item in data.get("writable_paths") or []],
            allow_network=bool(data.get("allow_network")) if "allow_network" in data else None,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed_commands": list(self.allowed_commands or []),
            "allowed_roots": list(self.allowed_roots or []),
            "allowed_env_keys": list(self.allowed_env_keys or []),
            "writable_paths": list(self.writable_paths or []),
            "allow_network": self.allow_network,
        }


@dataclasses.dataclass(frozen=True)
class ToolRequest:
    tool_id: str
    action: str
    parameters: Dict[str, Any]


@dataclasses.dataclass(frozen=True)
class ToolExecutionEnvelope:
    request: ToolRequest
    status: str
    output: Dict[str, Any]
    error: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request": {
                "tool_id": self.request.tool_id,
                "action": self.request.action,
                "parameters": self.request.parameters,
            },
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "metadata": self.metadata or {},
        }


@dataclasses.dataclass(frozen=True)
class WorkerResponse:
    status: str
    artifacts_created: List[Artifact]
    observations: List[Dict[str, Any]]
    recommendations: List[ToolRequest]
