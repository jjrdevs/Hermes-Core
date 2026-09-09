from __future__ import annotations
import json
from typing import Any, Callable, Dict, List, Optional
from engine.models import Artifact, ToolRequest, WorkerRequest, WorkerResponse
from .model_adapter import ModelAdapter, ToolExecutor


class LocalWorker:
    """Executes a single worker objective.

    Two execution paths:
      1. Native tool-calling (preferred): when the caller provides an
         OpenAI-style `tools` list AND a `tool_executor`, the worker
         drives `ModelAdapter.generate_with_tools(...)` which loops
         against the provider until the model stops emitting tool calls.
         Each emitted `ToolRequest` from the model is executed by `tool_executor`
         and fed back as a `role:"tool"` message.
      2. Legacy single-shot: when only a `ModelAdapter` is available, we
         make a single `generate(prompt)` call and — to preserve the
         existing kernel contract of "worker emits ToolRequest
         recommendations, kernel executes them under policy" — emit one
         `ToolRequest` per entry of `constraints.allowed_tools`.

    Both paths return `WorkerResponse(status="completed")` with the
    model's output recorded on the created `Artifact`.
    """

    def __init__(
        self,
        model_adapter: ModelAdapter,
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_executor: Optional[ToolExecutor] = None,
        max_iterations: int = 8,
    ) -> None:
        self.model_adapter = model_adapter
        self.tools = tools
        self.tool_executor = tool_executor
        self.max_iterations = max_iterations
        # Set to True only when execute() took the native tool-calling path.
        # Downstream (runtime_service._execute_step) checks this to skip the
        # legacy re-dispatch loop — because in the native path the adapter
        # already executed every tool call through tool_executor, so
        # re-dispatching the same calls via kernel.request_tool would run
        # FilesystemTool twice (double-execution bug).
        self.native_tools_used = False

    def execute(self, request: WorkerRequest) -> WorkerResponse:
        prompt = request.objective.get("description", "")
        allowed_tools = request.constraints.get("allowed_tools")
        expected_outputs = list(request.context.get("expected_outputs", []))
        artifact_type = expected_outputs[0] if len(expected_outputs) == 1 else "worker_output"

        observations: List[Dict[str, Any]] = []
        recommendations: List[ToolRequest] = []

        if self.tools and self.tool_executor is not None:
            self.native_tools_used = True
            messages: List[Dict[str, Any]] = [{"role": "user", "content": prompt}]
            # Optional system preamble from request.context.
            if isinstance(request.context.get("system"), str) and request.context.get("system"):
                messages.insert(0, {"role": "system", "content": request.context["system"]})
            result = self.model_adapter.generate_with_tools(
                messages,
                self.tools,
                tool_executor=self.tool_executor,
                max_iterations=self.max_iterations,
            )
            model_output = result.text
            # Translate every dispatched tool call into a ToolRequest
            # recommendation so downstream consumers (kernel, scheduler)
            # can log and/or re-execute them through the normal
            # approval policy.
            for tool_id, action, params, executor_json in result.tool_calls:
                recommendations.append(
                    ToolRequest(
                        tool_id=str(tool_id),
                        action=str(action),
                        parameters=params if isinstance(params, dict) else {"value": params},
                    )
                )
            observations.append(
                {"type": "log", "message": f"LocalWorker drove {result.iterations} model turn(s); "
                    f"{len(result.tool_calls)} tool call(s); finish_reason={result.finish_reason}."}
            )
            artifact_payload: Dict[str, Any] = {
                "prompt": prompt,
                "model_output": model_output,
                "tool_calls": result.tool_calls,
                "iterations": result.iterations,
                "finish_reason": result.finish_reason,
                "context": request.context,
                "constraints": request.constraints,
            }
        else:
            # Legacy single-shot path: one model call, one ToolRequest
            # per allowed tool id (the kernel decides whether to run
            # them and will gate through its approval policy).
            model_output = self.model_adapter.generate(prompt)
            if isinstance(allowed_tools, list) and allowed_tools:
                recommendations = [
                    ToolRequest(
                        tool_id=t,
                        action="invoke",
                        parameters={"prompt": prompt},
                    )
                    for t in allowed_tools
                ]
            observations.append({"type": "log", "message": "LocalWorker completed single-shot execution."})
            artifact_payload = {
                "prompt": prompt,
                "model_output": model_output,
                "context": request.context,
                "constraints": request.constraints,
            }

        artifact = Artifact.create(
            artifact_type=artifact_type,
            title=f"output_for_{request.execution_id}",
            content=artifact_payload,
            created_by=request.role,
            inputs=request.context.get("artifact_refs", []),
            parent_version=None,
            status="CREATED",
            decision_record={"reason": "generated_by_local_worker"},
            metadata={"worker_role": request.role},
        )
        return WorkerResponse(
            status="completed",
            artifacts_created=[artifact],
            observations=observations,
            recommendations=recommendations,
        )
