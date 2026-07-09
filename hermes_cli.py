#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from engine.models import StepDefinition, Tool, WorkflowDefinition, WorkerRequest
from engine.runtime import RuntimeKernel
from workers.local_worker import LocalWorker
from workers.model_adapter import StubModelAdapter

DEFAULT_DATA_DIR = Path.cwd() / ".hermes_data"
ALLOWED_EVENT_NAMES = {"STEP_COMPLETED", "STEP_EXECUTION_COMPLETED", "ARTIFACT_CREATED"}


def load_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


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
    root = Path(data_dir).resolve() if data_dir else DEFAULT_DATA_DIR
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


def print_workflow_summary(kernel: RuntimeKernel, workflow_execution_id: str) -> None:
    workflow_execution = kernel.workflow_executions[workflow_execution_id]
    current_steps = [kernel.step_executions[step_id] for step_id in workflow_execution.active_executions]
    completed_steps = [kernel.step_executions[step_id] for step_id in workflow_execution.completed_executions]
    failed_steps = [kernel.step_executions[step_id] for step_id in workflow_execution.failed_executions]
    approval_role = _pending_approval_role(kernel, workflow_execution)
    tool_activity = _tool_activity(kernel, workflow_execution)
    workflow_name = _get_workflow_name(kernel, workflow_execution)

    print(f"Workflow execution: {workflow_execution.execution_id}")
    print(f"Workflow name: {workflow_name}")
    print(f"Status: {workflow_execution.status}")
    print(f"Started: {workflow_execution.started_at}")
    if workflow_execution.completed_at:
        print(f"Completed: {workflow_execution.completed_at}")
    print(f"Current step(s): {', '.join(f'{step.step_id} ({step.capability_required})' for step in current_steps) if current_steps else 'none'}")
    print(f"Pending approval: {approval_role if approval_role is not None else 'none'}")
    print(f"Completed steps: {', '.join(step.step_id for step in completed_steps) if completed_steps else 'none'}")
    print(f"Failed steps: {', '.join(step.step_id for step in failed_steps) if failed_steps else 'none'}")
    print(f"Produced artifacts: {len(workflow_execution.produced_artifacts)}")
    if workflow_execution.produced_artifacts:
        for artifact_id in workflow_execution.produced_artifacts:
            artifact = kernel.artifact_store.get(artifact_id)
            if artifact is None:
                continue
            print(f"- {artifact.artifact_id}: {artifact.artifact_type} ({artifact.title})")
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


def execute_step(kernel: RuntimeKernel, step_execution_id: str) -> None:
    execution = kernel.step_executions[step_execution_id]
    step_definition = get_step_definition(kernel, step_execution_id)
    model_adapter = StubModelAdapter()
    capabilities = model_adapter.capabilities()
    decision = kernel.assign_execution(
        step_execution_id,
        worker_assigned={"worker_type": step_definition.role, "worker_id": "local-worker-1"},
        model_assigned={"adapter": capabilities["provider"], "model_name": capabilities["name"]},
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


def execute_workflow(kernel: RuntimeKernel, workflow_execution_id: str) -> None:
    while not kernel.workflow_complete(workflow_execution_id):
        step_execution_id = kernel.schedule_next_step(workflow_execution_id)

        if step_execution_id is None:
            if kernel.get_workflow_status(workflow_execution_id) == "WAITING_APPROVAL":
                return

            if kernel.workflow_complete(workflow_execution_id):
                break

            raise RuntimeError("Workflow is not complete and no ready steps remain")

        execute_step(kernel, step_execution_id)

    if kernel.workflow_complete(workflow_execution_id):
        kernel.complete_workflow(workflow_execution_id)


def command_validate(args: argparse.Namespace) -> int:
    path = Path(args.workflow_json).resolve()
    data = load_json_file(path)
    validate_workflow_schema(data)
    print(f"Workflow definition '{data.get('name')}' is valid.")
    return 0


def command_run(args: argparse.Namespace) -> int:
    path = Path(args.workflow_json).resolve()
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
    try:
        execute_workflow(kernel, workflow_execution_id)
    except Exception as exc:
        print(f"Execution failed: {exc}")
        kernel.shutdown()
        return 1
    print_workflow_summary(kernel, workflow_execution_id)
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
    try:
        execute_workflow(kernel, execution_id)
    except Exception as exc:
        print(f"Resume failed: {exc}")
        kernel.shutdown()
        return 1
    print_workflow_summary(kernel, execution_id)
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


def command_status(args: argparse.Namespace) -> int:
    paths = resolve_data_paths(args.data_dir)
    kernel = RuntimeKernel(paths["event_db"], paths["artifact_db"], paths["workflow_definition_db"])
    execution_id = args.workflow_execution_id
    if execution_id not in kernel.workflow_executions:
        print(f"Workflow execution '{execution_id}' not found.")
        kernel.shutdown()
        return 1
    print_workflow_summary(kernel, execution_id)
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


def main(argv: Optional[List[str]] = None) -> int:
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument("--data-dir", help="Directory for Hermes runtime persistence", default=None)

    parser = argparse.ArgumentParser(description="Hermes workflow runtime CLI", parents=[parent_parser])
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", parents=[parent_parser], help="Validate a workflow definition JSON file")
    validate_parser.add_argument("workflow_json", help="Path to workflow JSON")

    run_parser = subparsers.add_parser("run", parents=[parent_parser], help="Run a workflow definition JSON file")
    run_parser.add_argument("workflow_json", help="Path to workflow JSON")

    resume_parser = subparsers.add_parser("resume", parents=[parent_parser], help="Resume a persisted workflow execution")
    resume_parser.add_argument("workflow_execution_id", help="Workflow execution id")

    approve_parser = subparsers.add_parser("approve", parents=[parent_parser], help="Approve a waiting workflow execution")
    approve_parser.add_argument("workflow_execution_id", help="Workflow execution id")
    approve_parser.add_argument("--approved-by", help="Approver identifier", default="cli-user")
    approve_parser.add_argument("--reason", help="Approval reason", default="approved")
    approve_parser.add_argument("--comment", help="Approval comment", default=None)

    status_parser = subparsers.add_parser("status", parents=[parent_parser], help="Show status for a workflow execution")
    status_parser.add_argument("workflow_execution_id", help="Workflow execution id")

    artifacts_parser = subparsers.add_parser("artifacts", parents=[parent_parser], help="List artifacts for a workflow execution")
    artifacts_parser.add_argument("workflow_execution_id", help="Workflow execution id")

    list_parser = subparsers.add_parser("list", parents=[parent_parser], help="List persisted Hermes runtime resources")
    list_subparsers = list_parser.add_subparsers(dest="list_command", required=True)
    list_subparsers.add_parser("workflows", help="List persisted workflow definitions")
    list_subparsers.add_parser("executions", help="List persisted workflow executions")
    list_subparsers.add_parser("tools", help="List persisted tools")

    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            return command_validate(args)
        if args.command == "run":
            return command_run(args)
        if args.command == "resume":
            return command_resume(args)
        if args.command == "approve":
            return command_approve(args)
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
    except ValueError as exc:
        print(f"Validation error: {exc}")
        return 1
    return 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
