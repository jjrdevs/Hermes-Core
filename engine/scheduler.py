from __future__ import annotations

from typing import Any, Dict, List, Optional

from .models import StepDefinition, StepExecution, WorkflowDefinition, WorkflowExecution


class Scheduler:
    def __init__(
        self,
        workflow_definitions: Dict[str, WorkflowDefinition],
        workflow_executions: Dict[str, WorkflowExecution],
        step_executions: Dict[str, StepExecution],
        event_log: Any,
        artifact_store: Any,
    ) -> None:
        self.workflow_definitions = workflow_definitions
        self.workflow_executions = workflow_executions
        self.step_executions = step_executions
        self.event_log = event_log
        self.artifact_store = artifact_store

    def next_ready_step(self, workflow_execution_id: str) -> Optional[StepDefinition]:
        workflow_execution = self.workflow_executions[workflow_execution_id]
        workflow_definition = self.workflow_definitions[workflow_execution.workflow_definition_id]
        completed_steps = {self.step_executions[e].step_id for e in workflow_execution.completed_executions}

        for step in workflow_definition.steps:
            if step.id in completed_steps:
                continue
            if all(dep in completed_steps for dep in step.depends_on):
                return step

        return None

    def workflow_done(self, workflow_execution_id: str) -> bool:
        workflow_execution = self.workflow_executions[workflow_execution_id]
        workflow_definition = self.workflow_definitions[workflow_execution.workflow_definition_id]
        completed_steps = {self.step_executions[e].step_id for e in workflow_execution.completed_executions}
        return len(completed_steps) == len(workflow_definition.steps)

    def evaluate_workflow_transitions(self, workflow_execution_id: str) -> Optional[Dict[str, Any]]:
        workflow_execution = self.workflow_executions[workflow_execution_id]
        workflow_definition = self.workflow_definitions[workflow_execution.workflow_definition_id]
        return self._evaluate_transitions(workflow_definition, workflow_execution)

    def _evaluate_transitions(
        self,
        workflow_definition: WorkflowDefinition,
        workflow_execution: WorkflowExecution,
    ) -> Optional[Dict[str, Any]]:
        applicable_transitions: List[Dict[str, Any]] = []

        approval_granted = False
        for event_id in workflow_execution.events:
            event = self.event_log.get(event_id)
            if event is None:
                continue
            if event.event_type == "APPROVAL_GRANTED":
                approval_granted = True
            for transition in workflow_definition.transitions:
                if self._transition_matches_event(transition, event):
                    action = transition.get("action", {})
                    if approval_granted and "require_approval" in action:
                        continue
                    applicable_transitions.append(transition)

        if not applicable_transitions:
            return None

        applicable_transitions.sort(key=lambda transition: transition.get("priority", 0), reverse=True)
        action = applicable_transitions[0].get("action", {})

        if "schedule" in action:
            return {"type": "schedule", "role": action["schedule"].get("role")}
        if "require_approval" in action:
            return {"type": "require_approval", "role": action["require_approval"].get("role")}
        if "no_action" in action:
            return {"type": "no_action"}
        return None

    def _transition_matches_event(self, transition: Dict[str, Any], event: Any) -> bool:
        condition = transition.get("condition", {})
        condition_event = condition.get("event")
        step_completion_aliases = {"STEP_COMPLETED", "STEP_EXECUTION_COMPLETED", "WORKER_COMPLETED"}

        matches_event_type = condition_event == event.event_type
        if not matches_event_type and condition_event == "STEP_COMPLETED":
            matches_event_type = event.event_type in step_completion_aliases
        elif not matches_event_type and event.event_type == "STEP_COMPLETED":
            matches_event_type = condition_event in step_completion_aliases

        if not matches_event_type:
            return False

        artifact_type = condition.get("artifact_type")
        if artifact_type is None:
            return True

        if event.event_type == "ARTIFACT_CREATED":
            return artifact_type == event.payload.get("artifact_type")

        if event.event_type in {"STEP_EXECUTION_COMPLETED", "WORKER_COMPLETED", "STEP_COMPLETED"}:
            artifact_ids = event.payload.get("output_artifacts", [])
            for artifact_id in artifact_ids:
                artifact = self.artifact_store.get(artifact_id)
                if artifact is not None and artifact.artifact_type == artifact_type:
                    return True
            return False

        return False
