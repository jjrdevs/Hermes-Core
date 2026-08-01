from __future__ import annotations

import json
from typing import Any, Dict, Optional

from engine.api import HermesApi


class HermesHttpAdapter:
    def __init__(self, data_dir: Optional[str] = None, *, enabled: bool = False) -> None:
        self.api = HermesApi(data_dir=data_dir)
        self.enabled = enabled

    def handle(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        action = payload.get("action")
        if action == "run_workflow":
            return self.api.run_workflow(payload["workflow_path"], provider=payload.get("provider", "stub"), model_name=payload.get("model_name"), endpoint=payload.get("endpoint"))
        if action in {"start_run", "start"}:
            if not self.enabled:
                return {"accepted": False, "status": "disabled", "message": f"{action} is disabled for the legacy adapter"}
            return self.api.start_run(
                payload["workflow_path"],
                provider=payload.get("provider", "stub"),
                model_name=payload.get("model_name"),
                endpoint=payload.get("endpoint"),
                context=payload.get("context"),
            )
        if action in {"queue_run", "queue"}:
            if not self.enabled:
                return {"accepted": False, "status": "disabled", "message": f"{action} is disabled for the legacy adapter"}
            return self.api.queue_run(
                payload["workflow_path"],
                provider=payload.get("provider", "stub"),
                model_name=payload.get("model_name"),
                endpoint=payload.get("endpoint"),
                context=payload.get("context"),
            )
        if action in {"get_run", "status"}:
            if not self.enabled:
                return {"accepted": False, "status": "disabled", "message": f"{action} is disabled for the legacy adapter"}
            return self.api.get_run(payload["run_id"])
        if action in {"observe_run", "observe"}:
            if not self.enabled:
                return {"accepted": False, "status": "disabled", "message": f"{action} is disabled for the legacy adapter"}
            return self.api.observe_run(payload["run_id"])
        if action in {"get_run_artifacts", "artifacts"}:
            if not self.enabled:
                return {"accepted": False, "status": "disabled", "message": f"{action} is disabled for the legacy adapter"}
            return self.api.get_run_artifacts(payload["run_id"])
        if action == "list_checkpoints":
            return self.api.list_checkpoints()
        if action == "get_checkpoint":
            return self.api.get_checkpoint(payload["checkpoint_id"])
        if action in {"cancel_run", "cancel"}:
            if not self.enabled:
                return {"accepted": False, "status": "disabled", "message": f"{action} is disabled for the legacy adapter"}
            return self.api.cancel_run(payload["run_id"])
        if action in {"append_event", "append"}:
            if not self.enabled:
                return {"accepted": False, "status": "disabled", "message": f"{action} is disabled for the legacy adapter"}
            return self.api.append_event(payload["run_id"], payload.get("event", {}))
        if action in {"finalize_run", "finalize"}:
            if not self.enabled:
                return {"accepted": False, "status": "disabled", "message": f"{action} is disabled for the legacy adapter"}
            return self.api.finalize_run(payload["run_id"], payload.get("status"))
        if action in {"respond_approval", "approve"}:
            if not self.enabled:
                return {"accepted": False, "status": "disabled", "message": f"{action} is disabled for the legacy adapter"}
            return self.api.respond_approval(payload["run_id"], payload.get("choice", "approve"))
        if action in {"control", "handle_action"}:
            if not self.enabled:
                return {"accepted": False, "status": "disabled", "message": f"{action} is disabled for the legacy adapter"}
            return self.api.handle_action(
                payload["run_id"],
                payload.get("control_action") or payload.get("action_name") or payload.get("action"),
                payload.get("payload"),
                actor=payload.get("actor"),
                correlation_id=payload.get("correlation_id"),
            )
        if action == "get_execution_summary":
            return self.api.get_execution_summary(payload["execution_id"])
        if action == "get_artifacts":
            return self.api.get_artifacts(payload["execution_id"])
        raise ValueError(f"Unsupported action: {action}")

    def shutdown(self) -> None:
        self.api.shutdown()
