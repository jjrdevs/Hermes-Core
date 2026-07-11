from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from .models import (
    Event,
    StepDefinition,
    WorkflowDefinition,
    WorkerRequest,
)
from .runtime import RuntimeKernel
from workers.local_worker import LocalWorker
from workers.model_adapter import ModelAdapterConfig, ModelAdapterFactory


class RuntimeRunner:
    def __init__(self, event_db_path: Path, artifact_db_path: Path, model_config: Optional[ModelAdapterConfig] = None) -> None:
        self.kernel = RuntimeKernel(event_db_path, artifact_db_path)
        self.model_adapter = ModelAdapterFactory.create(model_config)

    def run_workflow(self, workflow_definition: WorkflowDefinition) -> str:
        self.kernel.register_workflow_definition(workflow_definition)
        workflow_execution_id = self.kernel.start_workflow(workflow_definition.workflow_definition_id)

        while not self.kernel.workflow_complete(workflow_execution_id):
            step_execution_id = self.kernel.schedule_next_step(workflow_execution_id)
            if step_execution_id is None:
                if self.kernel.get_workflow_status(workflow_execution_id) == "WAITING_APPROVAL":
                    return workflow_execution_id
                raise RuntimeError("Workflow is not complete and no ready steps remain")

            step_execution = self.kernel.step_executions[step_execution_id]
            stored_definition = self.kernel.workflow_definitions[step_execution.workflow_definition_id]
            step_definition = next(
                step for step in stored_definition.steps if step.id == step_execution.step_id
            )
            assign_decision = self.kernel.assign_execution(
                step_execution_id,
                model_adapter=self.model_adapter,
                capability=step_execution.capability_required,
                objective=step_definition.objective,
            )
            if not assign_decision.allowed:
                raise RuntimeError(f"Policy denied assignment: {assign_decision.reason}")

            start_decision = self.kernel.start_execution(step_execution_id)
            if not start_decision.allowed:
                raise RuntimeError(f"Policy denied start: {start_decision.reason}")

            worker_request = WorkerRequest(
                execution_id=step_execution_id,
                workflow_id=workflow_execution_id,
                role=step_execution.capability_required,
                objective=step_definition.objective,
                context={
                    "artifact_refs": step_execution.input_artifacts,
                    "expected_outputs": step_definition.outputs,
                },
                constraints=step_definition.constraints,
            )
            worker = LocalWorker(self.model_adapter)
            response = worker.execute(worker_request)
            if response.status != "completed":
                self.kernel.fail_execution(step_execution_id, "worker returned non-completed status", failure_type="worker")
                raise RuntimeError("Worker failed to complete the step")
            for recommendation in response.recommendations:
                if recommendation is None:
                    continue
                self.kernel.request_tool(
                    step_execution_id,
                    recommendation.tool_id,
                    recommendation.action,
                    recommendation.parameters,
                )
            try:
                self.kernel.complete_execution(step_execution_id, response.artifacts_created)
            except ValueError:
                raise

        self.kernel.complete_workflow(workflow_execution_id)
        return workflow_execution_id

    def shutdown(self) -> None:
        self.kernel.shutdown()
