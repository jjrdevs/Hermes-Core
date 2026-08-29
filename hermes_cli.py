#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from engine.runtime_service import RuntimeService

VERSION = "0.1.0"

from engine.models import StepDefinition, Tool, WorkflowDefinition, WorkerRequest
from engine.runtime import RuntimeKernel
from engine.scheduler import BackgroundScheduler, SQLiteJobStore
from engine.storage import SQLiteMemoryStore


def _format_job_status(row: Any) -> str:
    payload = row["payload"] if isinstance(row, dict) else {}
    execution_status = payload.get("execution_status") or {}
    checkpoint_id = execution_status.get("checkpoint_id") or payload.get("checkpoint")
    resume_hint = execution_status.get("resume_hint") or {}
    can_resume = bool(resume_hint.get("can_resume"))
    return f"status={row['status']} checkpoint={checkpoint_id or 'none'} resume={str(can_resume).lower()}"
from engine.workflow_loader import load_json_file, resolve_data_paths, resolve_input_path
from workers.local_worker import LocalWorker
from workers.model_adapter import ModelAdapterConfig, ModelAdapterFactory

DEFAULT_DATA_DIR = Path.home() / ".hermes" / "data"
ALLOWED_EVENT_NAMES = {"STEP_COMPLETED", "STEP_EXECUTION_COMPLETED", "ARTIFACT_CREATED"}


def validate_workflow_schema(data: Any) -> None:
    if not isinstance(data, dict):
        raise ValueError("Workflow JSON must be an object")

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Workflow definition must include a non-empty 'name'")

    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("Workflow definition must include a non-empty 'steps' list")

    step_ids: List[str] = []
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("Each step must be an object")
        step_id = step.get("id")
        if not isinstance(step_id, str) or not step_id.strip():
            raise ValueError("Each step must include a non-empty 'id'")
        if step_id in step_ids:
            raise ValueError(f"Duplicate step id found: {step_id}")
        step_ids.append(step_id)
        role = step.get("role")
        if not isinstance(role, str) or not role.strip():
            raise ValueError(f"Step '{step_id}' must include a non-empty 'role'")
        objective = step.get("objective")
        if not isinstance(objective, dict):
            raise ValueError(f"Step '{step_id}' must include an 'objective' object")
        depends_on = step.get("depends_on")
        if depends_on is None:
            raise ValueError(f"Step '{step_id}' must include a 'depends_on' list")
        if not isinstance(depends_on, list):
            raise ValueError(f"Step '{step_id}' depends_on must be a list")
        for dep in depends_on:
            if not isinstance(dep, str) or not dep.strip():
                raise ValueError(f"Step '{step_id}' has invalid dependency: {dep}")
        outputs = step.get("outputs")
        if outputs is None:
            raise ValueError(f"Step '{step_id}' must include an 'outputs' list")
        if not isinstance(outputs, list):
            raise ValueError(f"Step '{step_id}' outputs must be a list")
        for output in outputs:
            if not isinstance(output, str) or not output.strip():
                raise ValueError(f"Step '{step_id}' has invalid output: {output}")
        constraints = step.get("constraints")
        if not isinstance(constraints, dict):
            raise ValueError(f"Step '{step_id}' constraints must be an object")

    for step in steps:
        step_id = step["id"]
        for dep in step["depends_on"]:
            if dep not in step_ids:
                raise ValueError(f"Step '{step_id}' depends on unknown step '{dep}'")

    check_dependency_cycle(step_ids, steps)

    transitions = data.get("transitions", [])
    if not isinstance(transitions, list):
        raise ValueError("Transitions must be a list")
    for transition in transitions:
        if not isinstance(transition, dict):
            raise ValueError("Each transition must be an object")
        priority = transition.get("priority")
        if not isinstance(priority, int) or priority < 0:
            raise ValueError("Transition priority must be a non-negative integer")
        condition = transition.get("condition")
        if not isinstance(condition, dict):
            raise ValueError("Transition condition must be an object")
        event_name = condition.get("event")
        if not isinstance(event_name, str) or not event_name.strip():
            raise ValueError("Transition condition must include a non-empty 'event'")
        if event_name not in ALLOWED_EVENT_NAMES:
            raise ValueError(f"Unsupported transition event: {event_name}")
        artifact_type = condition.get("artifact_type")
        if artifact_type is not None and not isinstance(artifact_type, str):
            raise ValueError("Transition artifact_type must be a string")
        action = transition.get("action")
        if not isinstance(action, dict) or len(action) != 1:
            raise ValueError("Transition action must be an object with a single action")
        if "schedule" in action:
            schedule = action["schedule"]
            if not isinstance(schedule, dict):
                raise ValueError("schedule action must be an object")
            role = schedule.get("role")
            if not isinstance(role, str) or not role.strip():
                raise ValueError("schedule action must include a non-empty role")
        elif "require_approval" in action:
            require_approval = action["require_approval"]
            if not isinstance(require_approval, dict):
                raise ValueError("require_approval action must be an object")
            role = require_approval.get("role")
            if not isinstance(role, str) or not role.strip():
                raise ValueError("require_approval action must include a non-empty role")
        elif "no_action" in action:
            if action["no_action"] not in ({}, None):
                raise ValueError("no_action must be empty")
        else:
            raise ValueError(f"Unsupported transition action: {list(action.keys())}")

    tools = data.get("tools", [])
    if tools is not None:
        if not isinstance(tools, list):
            raise ValueError("tools must be a list")
        tool_ids: List[str] = []
        for tool in tools:
            if not isinstance(tool, dict):
                raise ValueError("Each tool must be an object")
            tool_id = tool.get("tool_id")
            if not isinstance(tool_id, str) or not tool_id.strip():
                raise ValueError("Each tool must include a non-empty 'tool_id'")
            if tool_id in tool_ids:
                raise ValueError(f"Duplicate tool id found: {tool_id}")
            tool_ids.append(tool_id)
            name = tool.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"Tool '{tool_id}' must include a non-empty 'name'")
            description = tool.get("description")
            if not isinstance(description, str):
                raise ValueError(f"Tool '{tool_id}' must include a description")
            actions = tool.get("actions")
            if not isinstance(actions, list) or not actions:
                raise ValueError(f"Tool '{tool_id}' must include a non-empty 'actions' list")
            for action_item in actions:
                if not isinstance(action_item, str):
                    raise ValueError(f"Tool '{tool_id}' action values must be strings")
            allowed_roles = tool.get("allowed_roles")
            if not isinstance(allowed_roles, list):
                raise ValueError(f"Tool '{tool_id}' allowed_roles must be a list")
            for role in allowed_roles:
                if not isinstance(role, str):
                    raise ValueError(f"Tool '{tool_id}' allowed_roles must contain strings")
            metadata = tool.get("metadata")
            if metadata is None:
                continue
            if not isinstance(metadata, dict):
                raise ValueError(f"Tool '{tool_id}' metadata must be an object")

    policy_refs = data.get("policy_refs", [])
    if not isinstance(policy_refs, list):
        raise ValueError("policy_refs must be a list")
    for policy_ref in policy_refs:
        if not isinstance(policy_ref, str):
            raise ValueError("policy_refs entries must be strings")


def check_dependency_cycle(step_ids: List[str], steps: List[Dict[str, Any]]) -> None:
    graph = {step["id"]: step["depends_on"] for step in steps}
    visited: Dict[str, str] = {}

    def visit(node: str) -> None:
        if visited.get(node) == "visiting":
            raise ValueError(f"Dependency cycle detected at step '{node}'")
        if visited.get(node) == "visited":
            return
        visited[node] = "visiting"
        for dep in graph.get(node, []):
            visit(dep)
        visited[node] = "visited"

    for step_id in step_ids:
        visit(step_id)


def build_workflow_definition(data: Dict[str, Any]) -> WorkflowDefinition:
    workflow_definition_id = data.get("workflow_definition_id")
    workflow_id = data.get("workflow_id")
    steps = [
        StepDefinition(
            id=step["id"],
            role=step["role"],
            objective=step["objective"],
            depends_on=step["depends_on"],
            outputs=step["outputs"],
            constraints=step["constraints"],
        )
        for step in data["steps"]
    ]
    workflow_definition = WorkflowDefinition.create(
        name=data["name"],
        steps=steps,
        transitions=data.get("transitions", []),
        policy_refs=data.get("policy_refs", []),
        workflow_id=workflow_id,
        workflow_definition_id=workflow_definition_id,
    )
    return workflow_definition


def build_tool_definitions(data: Dict[str, Any]) -> List[Tool]:
    tools: List[Tool] = []
    for tool_data in data.get("tools", []):
        tools.append(
            Tool(
                tool_id=tool_data["tool_id"],
                name=tool_data["name"],
                description=tool_data["description"],
                actions=tool_data["actions"],
                allowed_roles=tool_data["allowed_roles"],
                metadata=tool_data.get("metadata", {}),
            )
        )
    return tools


def resolve_data_paths(data_dir: Optional[str]) -> Dict[str, Path]:
    if data_dir is not None:
        root = Path(data_dir).expanduser()
    else:
        root = Path(os.environ.get("HERMES_DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser()
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return {
        "event_db": root / "events.db",
        "artifact_db": root / "artifacts.db",
        "workflow_definition_db": root / "workflow_definitions.db",
    }


def get_step_definition(kernel: RuntimeKernel, execution_id: str) -> StepDefinition:
    execution = kernel.step_executions[execution_id]
    workflow_definition = kernel.workflow_definitions[execution.workflow_definition_id]
    for step in workflow_definition.steps:
        if step.id == execution.step_id:
            return step
    raise RuntimeError(f"Step definition not found for execution {execution_id}")


def _get_workflow_name(kernel: RuntimeKernel, workflow_execution) -> str:
    workflow_definition = kernel.workflow_definitions.get(workflow_execution.workflow_definition_id)
    return workflow_definition.name if workflow_definition is not None else "<unknown>"


def _pending_approval_role(kernel: RuntimeKernel, workflow_execution) -> Optional[str]:
    if workflow_execution.status != "WAITING_APPROVAL":
        return None
    for event_id in reversed(workflow_execution.events):
        event = kernel.event_log.get(event_id)
        if event is None:
            continue
        if event.event_type == "APPROVAL_REQUIRED":
            return event.payload.get("role", "unknown")
    return "unknown"


def _tool_activity(kernel: RuntimeKernel, workflow_execution) -> Dict[str, int]:
    activity = {"requested": 0, "invoked": 0, "failed": 0}
    for event_id in workflow_execution.events:
        event = kernel.event_log.get(event_id)
        if event is None:
            continue
        if event.event_type == "TOOL_REQUESTED":
            activity["requested"] += 1
        elif event.event_type == "TOOL_INVOKED":
            activity["invoked"] += 1
        elif event.event_type == "TOOL_FAILED":
            activity["failed"] += 1
    return activity


def print_workflow_summary(kernel: RuntimeKernel, workflow_execution_id: str, *, data_dir: Optional[str] = None) -> None:
    workflow_execution = kernel.workflow_executions[workflow_execution_id]
    current_steps = [kernel.step_executions[step_id] for step_id in workflow_execution.active_executions]
    completed_steps = [kernel.step_executions[step_id] for step_id in workflow_execution.completed_executions]
    failed_steps = [kernel.step_executions[step_id] for step_id in workflow_execution.failed_executions]
    approval_role = _pending_approval_role(kernel, workflow_execution)
    tool_activity = _tool_activity(kernel, workflow_execution)
    workflow_name = _get_workflow_name(kernel, workflow_execution)

    print(f"Workflow execution: {workflow_execution.execution_id}")
    print(f"Workflow name: {workflow_name}")
    print(f"State: {workflow_execution.status}")
    print(f"Status: {workflow_execution.status}")
    print(f"Started: {workflow_execution.started_at}")
    if workflow_execution.completed_at:
        print(f"Completed: {workflow_execution.completed_at}")
    print(f"Current step(s): {', '.join(f'{step.step_id} ({step.capability_required})' for step in current_steps) if current_steps else 'none'}")
    print(f"Pending approval: {approval_role if approval_role is not None else 'none'}")
    print(f"Completed steps: {', '.join(step.step_id for step in completed_steps) if completed_steps else 'none'}")
    print(f"Failed steps: {', '.join(step.step_id for step in failed_steps) if failed_steps else 'none'}")
    print(f"Artifacts: {len(workflow_execution.produced_artifacts)}")
    if workflow_execution.produced_artifacts:
        for artifact_id in workflow_execution.produced_artifacts:
            artifact = kernel.artifact_store.get(artifact_id)
            if artifact is None:
                continue
            print(f"- {artifact.artifact_id}: {artifact.artifact_type} ({artifact.title})")

    latest_checkpoint = None
    try:
        service = RuntimeService(data_dir=data_dir)
        try:
            checkpoints = service.list_checkpoints()
            if checkpoints:
                latest_checkpoint = checkpoints[0]
        finally:
            service.shutdown()
    except Exception:
        latest_checkpoint = None

    if latest_checkpoint is None:
        print("Latest checkpoint: none")
    else:
        print(f"Latest checkpoint: {latest_checkpoint['checkpoint_id']} (iteration={latest_checkpoint.get('iteration', 0)})")
        summary = latest_checkpoint.get("summary", {})
        execution_loop = summary.get("execution_loop") or {}
        provider_routing = summary.get("provider_routing") or {}
        memory_context = summary.get("memory_context") or {}
        if execution_loop:
            print(f"Execution loop: phase={execution_loop.get('phase', 'unknown')} status={execution_loop.get('status', 'unknown')} stop_reason={execution_loop.get('stop_reason', 'unknown')} next_action={execution_loop.get('next_action', 'unknown')}")
        if provider_routing:
            print(f"Provider routing: provider={provider_routing.get('provider', 'unknown')} fallback_used={str(provider_routing.get('fallback_used', False)).lower()} reason={provider_routing.get('reason', 'unknown')}")
        if memory_context:
            print(f"Memory context: retrieval_topic={memory_context.get('retrieval_topic', 'unknown')} memory_count={memory_context.get('retrieved_count', 0)}")

    print("Tool activity: requested=%s invoked=%s failed=%s" % (tool_activity["requested"], tool_activity["invoked"], tool_activity["failed"]))


def print_artifacts(kernel: RuntimeKernel, workflow_execution_id: str) -> None:
    workflow_execution = kernel.workflow_executions[workflow_execution_id]
    artifact_ids = []
    for artifact_id in workflow_execution.produced_artifacts:
        if artifact_id not in artifact_ids:
            artifact_ids.append(artifact_id)

    if not artifact_ids:
        print("No artifacts produced yet.")
        return

    print(f"Artifacts for {workflow_execution.execution_id}:")
    for artifact_id in artifact_ids:
        artifact = kernel.artifact_store.get(artifact_id)
        if artifact is None:
            continue
        print(f"- artifact_id: {artifact.artifact_id}")
        print(f"  version: {artifact.version}")
        print(f"  type: {artifact.artifact_type}")
        print(f"  title: {artifact.title}")


def _build_model_config(args: Optional[argparse.Namespace] = None) -> ModelAdapterConfig:
    provider = "stub"
    model_name = None
    endpoint = None
    if args is not None:
        provider = args.provider or provider
        model_name = args.model_name or None
        endpoint = args.endpoint or None
    return ModelAdapterConfig(provider=provider, model_name=model_name, endpoint=endpoint)


def execute_step(kernel: RuntimeKernel, step_execution_id: str, model_adapter: Optional[Any] = None) -> None:
    execution = kernel.step_executions[step_execution_id]
    step_definition = get_step_definition(kernel, step_execution_id)
    model_adapter = model_adapter or ModelAdapterFactory.create(_build_model_config())
    capabilities = model_adapter.capabilities()
    decision = kernel.assign_execution(
        step_execution_id,
        model_adapter=model_adapter,
        capability=step_definition.role,
        objective=step_definition.objective,
    )
    if not decision.allowed:
        raise RuntimeError(f"Assignment denied: {decision.reason}")
    decision = kernel.start_execution(step_execution_id)
    if not decision.allowed:
        raise RuntimeError(f"Start denied: {decision.reason}")
    worker = LocalWorker(model_adapter)
    request = WorkerRequest(
        execution_id=step_execution_id,
        workflow_id=execution.workflow_id,
        role=step_definition.role,
        objective=step_definition.objective,
        context={
            "artifact_refs": execution.input_artifacts,
            "expected_outputs": step_definition.outputs,
        },
        constraints=step_definition.constraints,
    )
    response = worker.execute(request)
    for recommendation in response.recommendations:
        if recommendation is not None:
            kernel.request_tool(step_execution_id, recommendation.tool_id, recommendation.action, recommendation.parameters)
    kernel.complete_execution(step_execution_id, response.artifacts_created)


def execute_workflow(kernel: RuntimeKernel, workflow_execution_id: str, model_adapter: Optional[Any] = None) -> None:
    while not kernel.workflow_complete(workflow_execution_id):
        step_execution_id = kernel.schedule_next_step(workflow_execution_id)

        if step_execution_id is None:
            if kernel.get_workflow_status(workflow_execution_id) == "WAITING_APPROVAL":
                return

            if kernel.workflow_complete(workflow_execution_id):
                break

            raise RuntimeError("Workflow is not complete and no ready steps remain")

        execute_step(kernel, step_execution_id, model_adapter=model_adapter)

    if kernel.workflow_complete(workflow_execution_id):
        kernel.complete_workflow(workflow_execution_id)


def command_validate(args: argparse.Namespace) -> int:
    try:
        path = resolve_input_path(args.workflow_json)
    except FileNotFoundError as exc:
        print(str(exc))
        return 1
    data = load_json_file(path)
    validate_workflow_schema(data)
    print(f"Workflow definition '{data.get('name')}' is valid.")
    return 0


def command_run(args: argparse.Namespace) -> int:
    try:
        path = resolve_input_path(args.workflow_json)
    except FileNotFoundError as exc:
        print(str(exc))
        return 1
    data = load_json_file(path)
    validate_workflow_schema(data)
    workflow_definition = build_workflow_definition(data)
    tools = build_tool_definitions(data)
    paths = resolve_data_paths(args.data_dir)
    kernel = RuntimeKernel(paths["event_db"], paths["artifact_db"], paths["workflow_definition_db"])
    for tool in tools:
        kernel.register_tool(tool)
    kernel.register_workflow_definition(workflow_definition)
    workflow_execution_id = kernel.start_workflow(workflow_definition.workflow_definition_id)

    if args.dry_run:
        workflow_name = data.get("name") or workflow_definition.name
        print(f"Dry run preview for workflow '{workflow_name}'")
        print(f"Workflow execution: {workflow_execution_id}")
        print("No actions will be executed in dry-run mode.")
        for step in workflow_definition.steps:
            print(f"- planned step: {step.id} ({step.role})")
        kernel.shutdown()
        return 0

    model_adapter = ModelAdapterFactory.create(_build_model_config(args))
    try:
        execute_workflow(kernel, workflow_execution_id, model_adapter=model_adapter)
    except Exception as exc:
        print(f"Execution failed: {exc}")
        kernel.shutdown()
        return 1
    print_workflow_summary(kernel, workflow_execution_id, data_dir=args.data_dir)
    kernel.shutdown()
    return 0


def command_resume(args: argparse.Namespace) -> int:
    paths = resolve_data_paths(args.data_dir)
    kernel = RuntimeKernel(paths["event_db"], paths["artifact_db"], paths["workflow_definition_db"])
    execution_id = args.workflow_execution_id
    if execution_id not in kernel.workflow_executions:
        print(f"Workflow execution '{execution_id}' not found.")
        kernel.shutdown()
        return 1
    model_adapter = ModelAdapterFactory.create(_build_model_config(args))
    try:
        execute_workflow(kernel, execution_id, model_adapter=model_adapter)
    except Exception as exc:
        print(f"Resume failed: {exc}")
        kernel.shutdown()
        return 1
    print_workflow_summary(kernel, execution_id, data_dir=args.data_dir)
    kernel.shutdown()
    return 0


def command_approve(args: argparse.Namespace) -> int:
    paths = resolve_data_paths(args.data_dir)
    kernel = RuntimeKernel(paths["event_db"], paths["artifact_db"], paths["workflow_definition_db"])
    execution_id = args.workflow_execution_id
    if execution_id not in kernel.workflow_executions:
        print(f"Workflow execution '{execution_id}' not found.")
        kernel.shutdown()
        return 1
    try:
        kernel.approve_workflow(
            execution_id,
            approved_by=args.approved_by,
            reason=args.reason,
            comment=args.comment,
        )
    except ValueError as exc:
        print(f"Approval failed: {exc}")
        kernel.shutdown()
        return 1
    print(f"Approval recorded for workflow execution {execution_id}.")
    print(f"Run 'hermes resume {execution_id}' to continue execution.")
    print_workflow_summary(kernel, execution_id)
    kernel.shutdown()
    return 0


def command_approval_list(args: argparse.Namespace) -> int:
    service = RuntimeService(data_dir=args.data_dir)
    try:
        approvals = service.list_pending_tool_approvals(args.run_id)
    finally:
        service.shutdown()
    if not approvals:
        print("No pending tool approvals.")
        return 0
    print("Pending tool approvals:")
    for approval in approvals:
        print(
            f"- {approval['approval_id']}: run={approval['run_id']} tool={approval['tool_id']} "
            f"action={approval['action']} risk={approval['risk_level']}"
        )
        print(f"  parameters={json.dumps(approval['parameters'], sort_keys=True)}")
    return 0


def command_tool_approval_control(args: argparse.Namespace) -> int:
    service = RuntimeService(data_dir=args.data_dir)
    try:
        if args.approval_command == "approve":
            result = service.approve_tool_request(
                args.run_id,
                args.approval_id,
                approved_by=args.actor,
                reason=args.reason,
                comment=args.comment,
            )
        else:
            result = service.deny_tool_request(
                args.run_id,
                args.approval_id,
                denied_by=args.actor,
                reason=args.reason,
                comment=args.comment,
            )
    finally:
        service.shutdown()

    if not result.get("accepted"):
        print(f"Tool approval control failed: {result.get('message', 'unknown error')}")
        return 1
    print(f"Tool approval {args.approval_command}: {args.approval_id}")
    return 0


def command_status(args: argparse.Namespace) -> int:
    paths = resolve_data_paths(args.data_dir)
    kernel = RuntimeKernel(paths["event_db"], paths["artifact_db"], paths["workflow_definition_db"])
    execution_id = args.workflow_execution_id
    if execution_id not in kernel.workflow_executions:
        print(f"Workflow execution '{execution_id}' not found.")
        kernel.shutdown()
        return 1
    print_workflow_summary(kernel, execution_id, data_dir=args.data_dir)
    kernel.shutdown()
    return 0


def command_artifacts(args: argparse.Namespace) -> int:
    paths = resolve_data_paths(args.data_dir)
    kernel = RuntimeKernel(paths["event_db"], paths["artifact_db"], paths["workflow_definition_db"])
    execution_id = args.workflow_execution_id
    if execution_id not in kernel.workflow_executions:
        print(f"Workflow execution '{execution_id}' not found.")
        kernel.shutdown()
        return 1
    print_artifacts(kernel, execution_id)
    kernel.shutdown()
    return 0


def command_list_workflows(args: argparse.Namespace) -> int:
    paths = resolve_data_paths(args.data_dir)
    kernel = RuntimeKernel(paths["event_db"], paths["artifact_db"], paths["workflow_definition_db"])
    definitions = kernel.workflow_definition_store.list()
    if not definitions:
        print("No persisted workflow definitions.")
        kernel.shutdown()
        return 0
    print("Persisted workflow definitions:")
    for definition in definitions:
        print(f"- {definition.name} ({definition.workflow_definition_id}) version {definition.definition_version} steps={len(definition.steps)}")
    kernel.shutdown()
    return 0


def command_list_executions(args: argparse.Namespace) -> int:
    paths = resolve_data_paths(args.data_dir)
    kernel = RuntimeKernel(paths["event_db"], paths["artifact_db"], paths["workflow_definition_db"])
    executions = list(kernel.workflow_executions.values())
    if not executions:
        print("No persisted workflow executions.")
        kernel.shutdown()
        return 0
    print("Persisted workflow executions:")
    for execution in executions:
        wf_name = kernel.workflow_definitions.get(execution.workflow_definition_id).name if execution.workflow_definition_id in kernel.workflow_definitions else "<unknown>"
        print(f"- {execution.execution_id}: {wf_name} status={execution.status} started={execution.started_at}")
    kernel.shutdown()
    return 0


def command_list_tools(args: argparse.Namespace) -> int:
    paths = resolve_data_paths(args.data_dir)
    kernel = RuntimeKernel(paths["event_db"], paths["artifact_db"], paths["workflow_definition_db"])
    tools = kernel.tool_registry.list_tools()
    if not tools:
        print("No persisted tools registered.")
        kernel.shutdown()
        return 0
    print("Persisted tools:")
    for tool in tools:
        print(f"- {tool.tool_id}: {tool.name} allowed_roles={tool.allowed_roles} actions={tool.actions}")
    kernel.shutdown()
    return 0


def command_job_create(args: argparse.Namespace) -> int:
    paths = resolve_data_paths(args.data_dir)
    job_store = SQLiteJobStore(paths["event_db"].parent / "jobs.db")
    scheduler = BackgroundScheduler(job_store)
    job = scheduler.create_job(
        args.task,
        schedule=args.schedule,
        runtime_budget_seconds=int(args.runtime_budget_seconds),
    )
    print(f"{job['job_id']}: {job['task']} status={job['status']} schedule={job['schedule']}")
    job_store.close()
    return 0


def command_job_list(args: argparse.Namespace) -> int:
    paths = resolve_data_paths(args.data_dir)
    job_store = SQLiteJobStore(paths["event_db"].parent / "jobs.db")
    jobs = job_store.connection.execute("SELECT job_id, task, status, schedule, runtime_budget_seconds, payload FROM jobs ORDER BY created_at ASC").fetchall()
    if not jobs:
        print("No persisted jobs.")
        job_store.close()
        return 0
    print("Persisted jobs:")
    for row in jobs:
        payload = json.loads(row["payload"]) if row["payload"] else {}
        print(f"- {row['job_id']}: {row['task']} schedule={row['schedule']} budget={row['runtime_budget_seconds']} {_format_job_status({'status': row['status'], 'payload': payload})}")
    job_store.close()
    return 0


def command_job_get(args: argparse.Namespace) -> int:
    paths = resolve_data_paths(args.data_dir)
    job_store = SQLiteJobStore(paths["event_db"].parent / "jobs.db")
    row = job_store.connection.execute(
        "SELECT job_id, task, status, schedule, runtime_budget_seconds, payload FROM jobs WHERE job_id = ?",
        (args.job_id,),
    ).fetchone()
    if row is None:
        print(f"Job '{args.job_id}' not found.")
        job_store.close()
        return 1
    payload = json.loads(row["payload"]) if row["payload"] else {}
    print(f"job_id: {row['job_id']}")
    print(f"task: {row['task']}")
    print(f"status: {row['status']}")
    print(f"schedule: {row['schedule']}")
    print(f"budget: {row['runtime_budget_seconds']}")
    print(_format_job_status({"status": row["status"], "payload": payload}))
    if payload.get("execution_status"):
        print(json.dumps(payload["execution_status"], indent=2, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    job_store.close()
    return 0


def command_job_resume(args: argparse.Namespace) -> int:
    paths = resolve_data_paths(args.data_dir)
    job_store = SQLiteJobStore(paths["event_db"].parent / "jobs.db")
    scheduler = BackgroundScheduler(job_store)
    try:
        resumed = scheduler.resume_job(
            args.job_id,
            executor=lambda payload: {
                "status": "completed",
                "summary": f"Resumed from {payload.get('resume_from')}",
                "checkpoint": payload.get("resume_from"),
                "resume_hint": {"can_resume": False},
            },
        )
    except (KeyError, ValueError) as exc:
        print(str(exc))
        job_store.close()
        return 1

    print(f"Resumed job {args.job_id}: {resumed['summary']}")
    print(_format_job_status({"status": resumed["status"], "payload": resumed.get("payload", {})}))
    job_store.close()
    return 0


# ---------------------------------------------------------------------------
# spec-schedule — drive scheduled jobs declared in spec-sheets
# ---------------------------------------------------------------------------


def _sheets_dir_for(args: argparse.Namespace) -> str:
    """Resolve the spec-sheets directory from --data-dir (else default)."""
    if getattr(args, "data_dir", None):
        return str(Path(args.data_dir) / "spec-sheets")
    from engine.spec_scheduler import default_sheets_dir

    return default_sheets_dir()


def _json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def command_spec_list(args: argparse.Namespace) -> int:
    from engine.spec_scheduler import SpecScheduler

    scheduler = SpecScheduler(sheets_dir=_sheets_dir_for(args))
    sheets = scheduler.list_sheets()
    if _json(args):
        print(json.dumps({"sheets": sheets, "sheets_dir": str(scheduler.sheets_dir)}, indent=2))
    else:
        if not sheets:
            print(f"(no spec-sheets in {scheduler.sheets_dir})")
            return 0
        for s in sheets:
            print(s)
    return 0


def command_spec_show(args: argparse.Namespace) -> int:
    from engine.spec_scheduler import SpecScheduler

    scheduler = SpecScheduler(sheets_dir=_sheets_dir_for(args))
    sheet = scheduler.load_sheet(args.name)
    payload = {
        "name": sheet.source,
        "jobs": [
            {
                "name": spec.name,
                "workflow": spec.workflow,
                "schedule": spec.schedule,
                "goal": spec.goal,
                "provider": spec.provider,
                "model": spec.model_name,
                "endpoint": spec.endpoint,
                "require_approval": spec.require_approval,
                "budget": spec.budget,
            }
            for spec in sheet.jobs.values()
        ],
        "chains": [
            {
                "name": chain.name,
                "jobs": chain.jobs,
                "fail_strategy": chain.fail_strategy,
            }
            for chain in sheet.chains.values()
        ],
    }
    print(json.dumps(payload, indent=2))
    return 0


def command_spec_add(args: argparse.Namespace) -> int:
    from engine.spec_scheduler import SpecScheduler, SpecSheet

    scheduler = SpecScheduler(sheets_dir=_sheets_dir_for(args))
    name = args.name
    if not name.endswith(".json"):
        path = Path(scheduler.sheets_dir) / f"{name}.json"
    else:
        path = Path(scheduler.sheets_dir) / name

    target = {
        "name": args.job_name,
        "workflow": args.workflow,
        "schedule": args.schedule,
        "goal": args.goal,
        "provider": args.provider,
        "model_name": args.model_name,
        "endpoint": args.endpoint,
        "require_approval": args.require_approval,
        **({"context": json.loads(args.context)} if args.context else {}),
    }

    # Start from existing sheet (if any) as a plain data dict.
    data = {"name": name, "jobs": [], "chains": []}
    if path.exists():
        try:
            existing = SpecSheet.from_dict(source=str(path), data=json.loads(path.read_text()))
        except Exception as exc:  # noqa: BLE001 - surface the real error to the user
            print(f"Cannot read existing spec-sheet {path}: {exc}", file=sys.stderr)
            return 1
        for spec in existing.jobs.values():
            data["jobs"].append({
                "name": spec.name, "workflow": spec.workflow, "schedule": spec.schedule,
                "goal": spec.goal, "provider": spec.provider, "model_name": spec.model_name,
                "endpoint": spec.endpoint, "require_approval": spec.require_approval,
                **({"budget": spec.budget} if spec.budget else {}),
                **({"context": spec.context} if spec.context else {}),
            })
        for chain in existing.chains.values():
            data["chains"].append({"name": chain.name, "jobs": chain.jobs,
                                   "fail_strategy": chain.fail_strategy})

    # Replace or append the targeted job by name.
    data["jobs"] = [j for j in data["jobs"] if j["name"] != args.job_name]
    data["jobs"].append(target)

    SpecSheet.from_dict(source=str(path), data=data)  # validate before writing
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    print(f"Added job {args.job_name!r} to spec-sheet {name!r} at {path}")
    return 0


def command_spec_run(args: argparse.Namespace) -> int:
    from engine.spec_scheduler import STATUS_COMPLETED, SpecScheduler

    scheduler = SpecScheduler(sheets_dir=_sheets_dir_for(args))
    if args.chain:
        sheet_name = args.sheet or "default"
    else:
        if not args.job:
            print("spec-schedule run: specify --job or --chain", file=sys.stderr)
            return 2
        sheet_name = args.sheet or "default"
    try:
        if args.chain:
            outcome = scheduler.run_chain(sheet_name, args.chain)
            rows = outcome.to_dict()
        else:
            outcome = scheduler.run_job(sheet_name, args.job)
            rows = _outcome_to_dict_cli(outcome)
    except FileNotFoundError as exc:
        print(f"spec-schedule run: {exc}", file=sys.stderr)
        return 60  # usage: no such sheet
    except KeyError as exc:
        print(f"spec-schedule run: {exc}", file=sys.stderr)
        return 60
    if _json(args):
        print(json.dumps(rows, indent=2))
    else:
        print(f"status={rows.get('status')} run_id={rows.get('run_id') or '-'}")
        if rows.get("reason"):
            print(f"reason={rows['reason']}")
    if rows.get("status") and rows["status"] != STATUS_COMPLETED:
        return 1
    return 0


def command_spec_status(args: argparse.Namespace) -> int:
    from engine.spec_scheduler import SpecScheduler

    scheduler = SpecScheduler(sheets_dir=_sheets_dir_for(args))
    payload = scheduler.status()
    payload["sheets_dir"] = str(scheduler.sheets_dir)
    print(json.dumps(payload, indent=2))
    return 0


def _outcome_to_dict_cli(outcome) -> Dict[str, Any]:
    return {
        "job": outcome.job,
        "status": outcome.status,
        "run_id": outcome.run_id,
        "reason": outcome.reason,
        "duration_ms": outcome.duration_ms,
    }


def command_memory_list(args: argparse.Namespace) -> int:
    paths = resolve_data_paths(args.data_dir)
    memory_store = SQLiteMemoryStore(paths["event_db"].parent / "memory.db", max_entries=50)
    try:
        memories = memory_store.list(topic=args.topic, limit=args.limit)
        if not memories:
            print("No persisted memories.")
            return 0
        print("Persisted memories:")
        for memory in memories:
            print(f"- {memory['memory_id']}: topic={memory['topic']} type={memory['entry_type']} source={memory['source']}")
            print(f"  {memory['content']}")
            if memory.get("metadata"):
                print(f"  metadata={json.dumps(memory['metadata'], sort_keys=True)}")
        return 0
    finally:
        memory_store.close()


def command_memory_add(args: argparse.Namespace) -> int:
    paths = resolve_data_paths(args.data_dir)
    memory_store = SQLiteMemoryStore(paths["event_db"].parent / "memory.db", max_entries=50)
    try:
        metadata = {}
        if args.metadata:
            try:
                metadata = json.loads(args.metadata)
            except json.JSONDecodeError as exc:
                print(f"Invalid metadata JSON: {exc}")
                return 1
        if not isinstance(metadata, dict):
            print("Metadata must decode to a JSON object.")
            return 1

        memory = memory_store.add(
            args.entry_type,
            args.content,
            args.topic,
            args.source,
            metadata=metadata,
        )
        print(f"Stored memory {memory['memory_id']}: topic={memory['topic']} type={memory['entry_type']}")
        return 0
    finally:
        memory_store.close()


def command_memory_recall(args: argparse.Namespace) -> int:
    paths = resolve_data_paths(args.data_dir)
    memory_store = SQLiteMemoryStore(paths["event_db"].parent / "memory.db", max_entries=50)
    try:
        memories = memory_store.list(topic=args.topic, limit=args.limit)
        query_terms = [term.lower() for term in args.query.lower().split() if term]
        filtered = []
        for memory in memories:
            text = f"{memory['content']} {json.dumps(memory.get('metadata', {}), sort_keys=True)}".lower()
            if all(term in text for term in query_terms):
                filtered.append(memory)
        if not filtered:
            print("No matching memories.")
            return 0
        print("Matching memories:")
        for memory in filtered:
            print(f"- {memory['memory_id']}: topic={memory['topic']} type={memory['entry_type']} source={memory['source']}")
            print(f"  {memory['content']}")
            if memory.get("metadata"):
                print(f"  metadata={json.dumps(memory['metadata'], sort_keys=True)}")
        return 0
    finally:
        memory_store.close()


def command_checkpoint_list(args: argparse.Namespace) -> int:
    service = RuntimeService(data_dir=args.data_dir)
    try:
        checkpoints = service.list_checkpoints()
        if not checkpoints:
            print("No persisted checkpoints.")
            return 0
        print("Persisted checkpoints:")
        for checkpoint in checkpoints:
            summary = checkpoint.get("summary", {})
            progress = checkpoint.get("progress_summary") or {}
            task = summary.get("task") or checkpoint.get("last_action") or "<unknown>"
            phase = progress.get("phase", "unknown")
            stop_reason = progress.get("stop_reason", "unknown")
            status = checkpoint.get("execution_summary", {}).get("status", "UNKNOWN")
            print(f"- {checkpoint['checkpoint_id']}: task={task} iteration={checkpoint.get('iteration', 0)} status={status} phase={phase} stop_reason={stop_reason}")
        return 0
    finally:
        service.shutdown()


def command_checkpoint_get(args: argparse.Namespace) -> int:
    service = RuntimeService(data_dir=args.data_dir)
    try:
        checkpoint = service.get_checkpoint(args.checkpoint_id)
        if checkpoint is None:
            print(f"Checkpoint '{args.checkpoint_id}' not found.")
            return 1

        progress = checkpoint.get("progress_summary") or {}
        summary = checkpoint.get("summary", {})
        execution_loop = summary.get("execution_loop") or progress
        provider_routing = summary.get("provider_routing") or {}
        memory_context = summary.get("memory_context") or {}

        print(f"Checkpoint: {checkpoint['checkpoint_id']}")
        print(f"  task={progress.get('task') or summary.get('task') or '<unknown>'}")
        print(f"  iteration={checkpoint.get('iteration', 0)} status={progress.get('status', 'unknown')} phase={progress.get('phase', 'unknown')}")
        print(f"  stop_reason={progress.get('stop_reason', 'unknown')}")
        print(f"  next_action={progress.get('next_action', 'unknown')}")
        if provider_routing:
            print(f"  provider={provider_routing.get('provider', 'unknown')} fallback_used={str(provider_routing.get('fallback_used', False)).lower()}")
        if memory_context:
            print(f"  memory_count={memory_context.get('retrieved_count', 0)} retrieval_topic={memory_context.get('retrieval_topic', 'unknown')}")
        if execution_loop:
            print(f"  verification_status={execution_loop.get('verification_status', 'unknown')}")
        return 0
    finally:
        service.shutdown()


def command_rollback(args: argparse.Namespace) -> int:
    service = RuntimeService(data_dir=args.data_dir)
    try:
        result = service.rollback_checkpoint(args.checkpoint_id, actor=args.actor, reason=args.reason)
    finally:
        service.shutdown()

    if result["status"] == "ROLLED_BACK":
        paths = result.get("paths") or ([result["path"]] if result.get("path") else [])
        print(f"Rolled back checkpoint {args.checkpoint_id}: {', '.join(paths)}")
        return 0
    print(f"Rollback {result['status'].lower()}: {result.get('reason', 'unknown reason')}")
    return 1


def command_change_get(args: argparse.Namespace) -> int:
    service = RuntimeService(data_dir=args.data_dir)
    try:
        change = service.get_change_record(args.checkpoint_id)
    finally:
        service.shutdown()
    if change is None:
        print(f"Change record for checkpoint '{args.checkpoint_id}' not found.")
        return 1
    patch_result = change.get("patch_result") or {}
    print(f"Change record: {change['checkpoint_id']}")
    print(f"  task={change.get('task') or '<unknown>'}")
    print(f"  applied={str(patch_result.get('applied', False)).lower()} reason={patch_result.get('reason', 'unknown')}")
    changes = patch_result.get("changes") or [patch_result]
    for item in changes:
        print(f"  path={item.get('path', '<unknown>')}")
        print(f"    before_hash={item.get('before_hash', 'unknown')} after_hash={item.get('after_hash', 'unknown')}")
        if item.get("diff"):
            print("    diff:")
            for line in str(item["diff"]).splitlines():
                print(f"      {line}")
    verification = change.get("verification") or {}
    print(f"  verification={verification.get('status', 'unknown')}")
    if change.get("rollback"):
        print(f"  rollback={json.dumps(change['rollback'], sort_keys=True)}")
    return 0


def command_policy_denials(args: argparse.Namespace) -> int:
    service = RuntimeService(data_dir=args.data_dir)
    try:
        denials = service.list_policy_denials(args.run_id)
    finally:
        service.shutdown()
    if not denials:
        print("No policy denials.")
        return 0
    print("Policy denials:")
    for denial in denials:
        print(
            f"- {denial['event_id']}: run={denial['run_id']} tool={denial.get('tool_id') or 'unknown'} "
            f"action={denial.get('action') or 'unknown'} reason={denial.get('reason') or 'unknown'}"
        )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument("--data-dir", help="Directory for Hermes runtime persistence", default=None)

    parser = argparse.ArgumentParser(description="Hermes workflow runtime CLI", parents=[parent_parser])
    parser.add_argument("--version", action="store_true", help="Print Hermes Core version and exit")
    subparsers = parser.add_subparsers(dest="command")

    validate_parser = subparsers.add_parser("validate", parents=[parent_parser], help="Validate a workflow definition JSON file")
    validate_parser.add_argument("workflow_json", help="Path to workflow JSON")

    run_parser = subparsers.add_parser("run", parents=[parent_parser], help="Run a workflow definition JSON file")
    run_parser.add_argument("workflow_json", help="Path to workflow JSON")
    run_parser.add_argument("--provider", help="Model adapter provider (stub or ollama)", default="stub")
    run_parser.add_argument("--model-name", help="Model name to use for the adapter", default=None)
    run_parser.add_argument("--endpoint", help="Remote endpoint for adapter providers", default=None)
    run_parser.add_argument("--dry-run", action="store_true", help="Preview workflow actions without executing them")

    resume_parser = subparsers.add_parser("resume", parents=[parent_parser], help="Resume a persisted workflow execution")
    resume_parser.add_argument("workflow_execution_id", help="Workflow execution id")
    resume_parser.add_argument("--provider", help="Model adapter provider (stub or ollama)", default="stub")
    resume_parser.add_argument("--model-name", help="Model name to use for the adapter", default=None)
    resume_parser.add_argument("--endpoint", help="Remote endpoint for adapter providers", default=None)
    resume_parser.add_argument("--dry-run", action="store_true", help="Preview workflow resume actions without executing them")

    approve_parser = subparsers.add_parser("approve", parents=[parent_parser], help="Approve a waiting workflow execution")
    approve_parser.add_argument("workflow_execution_id", help="Workflow execution id")
    approve_parser.add_argument("--approved-by", help="Approver identifier", default="cli-user")
    approve_parser.add_argument("--reason", help="Approval reason", default="approved")
    approve_parser.add_argument("--comment", help="Approval comment", default=None)

    approval_parser = subparsers.add_parser("approval", parents=[parent_parser], help="Inspect pending tool approvals")
    approval_subparsers = approval_parser.add_subparsers(dest="approval_command", required=True)
    approval_list_parser = approval_subparsers.add_parser("list", parents=[parent_parser], help="List pending tool approvals")
    approval_list_parser.add_argument("--run-id", help="Limit results to one run", default=None)
    for approval_action in ("approve", "deny"):
        control_parser = approval_subparsers.add_parser(approval_action, parents=[parent_parser], help=f"{approval_action.title()} a pending tool approval")
        control_parser.add_argument("run_id", help="Run identifier")
        control_parser.add_argument("approval_id", help="Pending tool approval identifier")
        control_parser.add_argument("--actor", default="cli-user", help="Actor making the decision")
        control_parser.add_argument("--reason", default=approval_action, help="Decision reason")
        control_parser.add_argument("--comment", default=None, help="Optional decision comment")

    status_parser = subparsers.add_parser("status", parents=[parent_parser], help="Show status for a workflow execution")
    status_parser.add_argument("workflow_execution_id", help="Workflow execution id")

    artifacts_parser = subparsers.add_parser("artifacts", parents=[parent_parser], help="List artifacts for a workflow execution")
    artifacts_parser.add_argument("workflow_execution_id", help="Workflow execution id")

    list_parser = subparsers.add_parser("list", parents=[parent_parser], help="List persisted Hermes runtime resources")
    list_subparsers = list_parser.add_subparsers(dest="list_command", required=True)
    list_subparsers.add_parser("workflows", parents=[parent_parser], help="List persisted workflow definitions")
    list_subparsers.add_parser("executions", parents=[parent_parser], help="List persisted workflow executions")
    list_subparsers.add_parser("tools", parents=[parent_parser], help="List persisted tools")

    job_parser = subparsers.add_parser("job", parents=[parent_parser], help="Manage background jobs")
    job_subparsers = job_parser.add_subparsers(dest="job_command", required=True)
    job_create_parser = job_subparsers.add_parser("create", parents=[parent_parser], help="Create a background job")
    job_create_parser.add_argument("task", help="Task description for the job")
    job_create_parser.add_argument("--schedule", help="Schedule for the job", default="manual")
    job_create_parser.add_argument("--runtime-budget-seconds", help="Maximum runtime budget for the job", default="60")
    job_subparsers.add_parser("list", parents=[parent_parser], help="List persisted background jobs")
    job_get_parser = job_subparsers.add_parser("get", parents=[parent_parser], help="Show a persisted background job")
    job_get_parser.add_argument("job_id", help="Job identifier")
    job_resume_parser = job_subparsers.add_parser("resume", parents=[parent_parser], help="Resume a persisted background job")
    job_resume_parser.add_argument("job_id", help="Job identifier")

    spec_parser = subparsers.add_parser("spec-schedule", parents=[parent_parser], help="List, inspect, add to, and run spec-sheet schedules")
    spec_subparsers = spec_parser.add_subparsers(dest="spec_command", required=True)
    spec_subparsers.add_parser("list", parents=[parent_parser], help="List spec-sheets").add_argument("--json", action="store_true")
    spec_show_p = spec_subparsers.add_parser("show", parents=[parent_parser], help="Show a spec-sheet's jobs and chains")
    spec_show_p.add_argument("name", help="Spec-sheet name (with or without .json)")
    spec_add_p = spec_subparsers.add_parser("add", parents=[parent_parser], help="Add (or replace) a job on a spec-sheet")
    spec_add_p.add_argument("name", help="Spec-sheet name (with or without .json)")
    spec_add_p.add_argument("job_name", help="Name of the job to add/replace")
    spec_add_p.add_argument("--workflow", required=True, help="Workflow JSON path or id the job runs")
    spec_add_p.add_argument("--schedule", default="manual", help="manual | now | interval:<s> | at:<iso> | cron:<5 fields>")
    spec_add_p.add_argument("--goal", default=None)
    spec_add_p.add_argument("--provider", default=None)
    spec_add_p.add_argument("--model-name", default=None)
    spec_add_p.add_argument("--endpoint", default=None)
    spec_add_p.add_argument("--require-approval", action="store_true", default=False)
    spec_add_p.add_argument("--context", default=None, help="JSON string of extra context for the job")
    spec_run_p = spec_subparsers.add_parser("run", parents=[parent_parser], help="Run a single job or chain on demand")
    spec_run_p.add_argument("--sheet", default=None, help="Spec-sheet name (default: 'default')")
    spec_run_p.add_argument("--job", default=None, help="Job name to run")
    spec_run_p.add_argument("--chain", default=None, help="Chain name to run")
    spec_run_p.add_argument("--json", action="store_true")
    spec_subparsers.add_parser("status", parents=[parent_parser], help="Show scheduler status and next-due map")

    memory_parser = subparsers.add_parser("memory", parents=[parent_parser], help="Inspect persisted memory entries")
    memory_subparsers = memory_parser.add_subparsers(dest="memory_command", required=True)
    memory_list_parser = memory_subparsers.add_parser("list", parents=[parent_parser], help="List persisted memories")
    memory_list_parser.add_argument("--topic", help="Filter memory entries by topic", default=None)
    memory_list_parser.add_argument("--limit", help="Maximum number of memories to return", type=int, default=10)
    memory_add_parser = memory_subparsers.add_parser("add", parents=[parent_parser], help="Store a new memory entry")
    memory_add_parser.add_argument("entry_type", help="Memory entry type", default="lesson")
    memory_add_parser.add_argument("content", help="Memory content to persist")
    memory_add_parser.add_argument("--topic", help="Topic for the memory", default="default")
    memory_add_parser.add_argument("--source", help="Source of the memory", default="cli")
    memory_add_parser.add_argument("--metadata", help="Optional JSON metadata object", default=None)
    memory_recall_parser = memory_subparsers.add_parser("recall", parents=[parent_parser], help="Recall persisted memories by query")
    memory_recall_parser.add_argument("query", help="Keyword or phrase to search for")
    memory_recall_parser.add_argument("--topic", help="Filter memory entries by topic", default=None)
    memory_recall_parser.add_argument("--limit", help="Maximum number of memories to inspect", type=int, default=10)

    checkpoint_parser = subparsers.add_parser("checkpoint", parents=[parent_parser], help="Inspect persisted checkpoints")
    checkpoint_subparsers = checkpoint_parser.add_subparsers(dest="checkpoint_command", required=True)
    checkpoint_subparsers.add_parser("list", parents=[parent_parser], help="List persisted checkpoints")
    checkpoint_get_parser = checkpoint_subparsers.add_parser("get", parents=[parent_parser], help="Show a persisted checkpoint")
    checkpoint_get_parser.add_argument("checkpoint_id", help="Checkpoint identifier")

    rollback_parser = subparsers.add_parser("rollback", parents=[parent_parser], help="Rollback an applied task edit from a checkpoint")
    rollback_parser.add_argument("checkpoint_id", help="Checkpoint identifier")
    rollback_parser.add_argument("--actor", default="cli-user", help="Actor performing the rollback")
    rollback_parser.add_argument("--reason", default="rollback requested from CLI", help="Reason for the rollback")

    change_parser = subparsers.add_parser("change", parents=[parent_parser], help="Inspect persisted task change records")
    change_subparsers = change_parser.add_subparsers(dest="change_command", required=True)
    change_get_parser = change_subparsers.add_parser("get", parents=[parent_parser], help="Show a task change record")
    change_get_parser.add_argument("checkpoint_id", help="Checkpoint identifier")

    policy_parser = subparsers.add_parser("policy", parents=[parent_parser], help="Inspect policy decisions")
    policy_subparsers = policy_parser.add_subparsers(dest="policy_command", required=True)
    denial_parser = policy_subparsers.add_parser("denials", parents=[parent_parser], help="List policy-denied actions")
    denial_parser.add_argument("--run-id", help="Limit results to one run", default=None)

    args = parser.parse_args(argv)
    if args.version:
        print(f"Hermes Core {VERSION}")
        return 0
    if args.command is None:
        parser.print_help()
        return 1
    try:
        if args.command == "validate":
            return command_validate(args)
        if args.command == "run":
            return command_run(args)
        if args.command == "resume":
            return command_resume(args)
        if args.command == "approve":
            return command_approve(args)
        if args.command == "approval" and args.approval_command == "list":
            return command_approval_list(args)
        if args.command == "approval" and args.approval_command in {"approve", "deny"}:
            return command_tool_approval_control(args)
        if args.command == "status":
            return command_status(args)
        if args.command == "artifacts":
            return command_artifacts(args)
        if args.command == "list":
            if args.list_command == "workflows":
                return command_list_workflows(args)
            if args.list_command == "executions":
                return command_list_executions(args)
            if args.list_command == "tools":
                return command_list_tools(args)
        if args.command == "job":
            if args.job_command == "create":
                return command_job_create(args)
            if args.job_command == "list":
                return command_job_list(args)
            if args.job_command == "get":
                return command_job_get(args)
            if args.job_command == "resume":
                return command_job_resume(args)
        if args.command == "spec-schedule":
            if args.spec_command == "list":
                return command_spec_list(args)
            if args.spec_command == "show":
                return command_spec_show(args)
            if args.spec_command == "add":
                return command_spec_add(args)
            if args.spec_command == "run":
                return command_spec_run(args)
            if args.spec_command == "status":
                return command_spec_status(args)
        if args.command == "memory":
            if args.memory_command == "list":
                return command_memory_list(args)
            if args.memory_command == "add":
                return command_memory_add(args)
            if args.memory_command == "recall":
                return command_memory_recall(args)
        if args.command == "checkpoint":
            if args.checkpoint_command == "list":
                return command_checkpoint_list(args)
            if args.checkpoint_command == "get":
                return command_checkpoint_get(args)
        if args.command == "rollback":
            return command_rollback(args)
        if args.command == "change" and args.change_command == "get":
            return command_change_get(args)
        if args.command == "policy" and args.policy_command == "denials":
            return command_policy_denials(args)
    except ValueError as exc:
        print(f"Validation error: {exc}")
        return 1
    return 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
