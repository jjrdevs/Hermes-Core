from __future__ import annotations

from typing import Dict, List

from engine.models import Artifact, ToolRequest, WorkerRequest, WorkerResponse
from .model_adapter import ModelAdapter


class LocalWorker:
    def __init__(self, model_adapter: ModelAdapter) -> None:
        self.model_adapter = model_adapter

    def execute(self, request: WorkerRequest) -> WorkerResponse:
        prompt = request.objective.get("description", "")
        model_output = self.model_adapter.generate(prompt)
        expected_outputs = request.context.get("expected_outputs", [])
        artifact_type = expected_outputs[0] if len(expected_outputs) == 1 else "worker_output"
        artifact = Artifact.create(
            artifact_type=artifact_type,
            title=f"output_for_{request.execution_id}",
            content={
                "prompt": prompt,
                "model_output": model_output,
                "context": request.context,
                "constraints": request.constraints,
            },
            created_by=request.role,
            inputs=request.context.get("artifact_refs", []),
            parent_version=None,
            status="CREATED",
            decision_record={"reason": "generated_by_local_worker"},
            metadata={"worker_role": request.role},
        )
        recommendations = []
        allowed_tools = request.constraints.get("allowed_tools")
        if isinstance(allowed_tools, list) and allowed_tools:
            recommendations.append(
                ToolRequest(
                    tool_id=allowed_tools[0],
                    action="read",
                    parameters={"query": "Gather support materials from the tool."},
                )
            )
        return WorkerResponse(
            status="completed",
            artifacts_created=[artifact],
            observations=[{"type": "log", "message": "LocalWorker completed execution."}],
            recommendations=recommendations,
        )
