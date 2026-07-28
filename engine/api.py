from __future__ import annotations

from typing import Any, Dict, Optional

from engine.runtime_service import RuntimeService


class HermesApi:
    def __init__(self, data_dir: Optional[str] = None) -> None:
        self.service = RuntimeService(data_dir=data_dir)

    def run_workflow(self, workflow_path: str, *, provider: str = "stub", model_name: Optional[str] = None, endpoint: Optional[str] = None) -> Dict[str, Any]:
        return self.service.run_workflow(workflow_path, provider=provider, model_name=model_name, endpoint=endpoint)

    def start_run(
        self,
        workflow_path: str,
        *,
        provider: str = "stub",
        model_name: Optional[str] = None,
        endpoint: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self.service.start_run(workflow_path, provider=provider, model_name=model_name, endpoint=endpoint, context=context)

    def queue_run(
        self,
        workflow_path: str,
        *,
        provider: str = "stub",
        model_name: Optional[str] = None,
        endpoint: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self.service.queue_run(workflow_path, provider=provider, model_name=model_name, endpoint=endpoint, context=context)

    def get_run(self, run_id: str) -> Dict[str, Any]:
        return self.service.get_run(run_id)

    def observe_run(self, run_id: str) -> Dict[str, Any]:
        return self.service.observe_run(run_id)

    def get_execution_summary(self, execution_id: str) -> Dict[str, Any]:
        return self.service.get_execution_summary(execution_id)

    def get_artifacts(self, execution_id: str) -> list[Dict[str, Any]]:
        return self.service.get_artifacts(execution_id)

    def get_run_artifacts(self, run_id: str) -> list[Dict[str, Any]]:
        return self.service.get_run_artifacts(run_id)

    def cancel_run(self, run_id: str) -> Dict[str, Any]:
        return self.service.cancel_run(run_id)

    def handle(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        action = str(payload.get("action", "")).strip().lower()
        if action == "run_workflow":
            return self.run_workflow(
                payload["workflow_path"],
                provider=payload.get("provider", "stub"),
                model_name=payload.get("model_name"),
                endpoint=payload.get("endpoint"),
            )
        if action in {"start_run", "start"}:
            return self.start_run(
                payload["workflow_path"],
                provider=payload.get("provider", "stub"),
                model_name=payload.get("model_name"),
                endpoint=payload.get("endpoint"),
                context=payload.get("context"),
            )
        if action in {"queue_run", "queue"}:
            return self.queue_run(
                payload["workflow_path"],
                provider=payload.get("provider", "stub"),
                model_name=payload.get("model_name"),
                endpoint=payload.get("endpoint"),
                context=payload.get("context"),
            )
        if action in {"get_run", "status"}:
            return self.get_run(payload["run_id"])
        if action in {"observe_run", "observe"}:
            return self.observe_run(payload["run_id"])
        if action in {"get_run_artifacts", "artifacts"}:
            return self.get_run_artifacts(payload["run_id"])
        if action in {"cancel_run", "cancel"}:
            return self.cancel_run(payload["run_id"])
        if action in {"append_event", "append"}:
            return self.append_event(payload["run_id"], payload.get("event", {}))
        if action in {"finalize_run", "finalize"}:
            return self.finalize_run(payload["run_id"], payload.get("status"))
        if action in {"respond_approval", "approve"}:
            return self.respond_approval(payload["run_id"], payload.get("choice", "approve"))
        if action in {"control", "handle_action"}:
            return self.handle_action(
                payload["run_id"],
                payload.get("control_action") or payload.get("action_name") or payload.get("action"),
                payload.get("payload"),
                actor=payload.get("actor"),
                correlation_id=payload.get("correlation_id"),
            )
        if action == "get_execution_summary":
            return self.get_execution_summary(payload["execution_id"])
        if action == "get_artifacts":
            return self.get_artifacts(payload["execution_id"])
        raise ValueError(f"Unsupported action: {action}")

    def append_event(self, run_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        return self.service.append_event(run_id, event)

    def finalize_run(self, run_id: str, status: Optional[str] = None) -> Dict[str, Any]:
        return self.service.finalize_run(run_id, status)

    def respond_approval(self, run_id: str, choice: str) -> Dict[str, Any]:
        return self.service.respond_approval(run_id, choice)

    def handle_action(
        self,
        run_id: str,
        action: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        actor: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.service.handle_action(run_id, action, payload, actor=actor, correlation_id=correlation_id)

    def __enter__(self) -> "HermesApi":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.shutdown()

    def shutdown(self) -> None:
        self.service.shutdown()
