from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from engine.runtime_service import RuntimeService


def _spec_job_outcome(o: Any) -> Dict[str, Any]:
    """Serialize a ``JobOutcome`` the same way the CLI does."""
    return {
        "sheet": o.sheet,
        "job": o.job,
        "status": o.status,
        "run_id": o.run_id,
        "reason": o.reason,
        "duration_ms": o.duration_ms,
        "started_at": o.started_at.isoformat() if o.started_at else None,
        "finished_at": o.finished_at.isoformat() if o.finished_at else None,
    }


class HermesApi:
    def __init__(self, data_dir: Optional[str] = None) -> None:
        self.service = RuntimeService(data_dir=data_dir)
        # Sheet store sits under the same root as the service (matches the
        # CLI's ``--data-dir`` -> spec-sheets/ convention and the supervisor's
        # default_sheets_dir()).
        self.data_dir = data_dir

    def run_workflow(
        self,
        workflow_path: str,
        *,
        provider: str = "stub",
        model_name: Optional[str] = None,
        endpoint: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self.service.run_workflow(
            workflow_path,
            provider=provider,
            model_name=model_name,
            endpoint=endpoint,
            context=context,
        )

    def run_task(self, task: str, *, workspace_path: Optional[str] = None, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.service.run_task(task, workspace_path=workspace_path, context=context)

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

    def get_run_progress(self, run_id: str) -> Dict[str, Any]:
        return self.service.get_run_progress(run_id)

    # ------------------------------------------------------------------
    # Spec-schedule surface (P3). The webui reaches these through the
    # ``adapter -> HermesCoreClient.handle -> HermesApi`` seam; it never
    # imports ``engine.spec_scheduler`` directly, so the core stays the
    # single source of truth for sheet parsing, execution, and status.
    # ------------------------------------------------------------------

    def _spec_scheduler(self, sheets_dir: Optional[str] = None) -> Any:
        from engine.spec_scheduler import SpecScheduler, default_data_dir

        if sheets_dir:
            return SpecScheduler(sheets_dir=sheets_dir)
        base = self.data_dir or default_data_dir()
        return SpecScheduler(sheets_dir=str(Path(base) / "spec-sheets"))

    def spec_list(self, *, sheets_dir: Optional[str] = None) -> Dict[str, Any]:
        s = self._spec_scheduler(sheets_dir)
        return {"sheets": s.list_sheets(), "sheets_dir": str(s.sheets_dir)}

    def spec_show(self, name: str, *, sheets_dir: Optional[str] = None) -> Dict[str, Any]:
        from engine.spec_scheduler import SpecSheet

        s = self._spec_scheduler(sheets_dir)
        sheet = s.load_sheet(name)
        return {
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
                    **({"context": spec.context} if spec.context else {}),
                }
                for spec in sheet.jobs.values()
            ],
            "chains": [
                {"name": c.name, "jobs": c.jobs, "fail_strategy": c.fail_strategy}
                for c in sheet.chains.values()
            ],
        }

    def spec_add(
        self,
        name: str,
        job_name: str,
        workflow: str,
        *,
        schedule: str = "manual",
        goal: Optional[str] = None,
        provider: Optional[str] = None,
        model_name: Optional[str] = None,
        endpoint: Optional[str] = None,
        require_approval: bool = False,
        context: Optional[Dict[str, Any]] = None,
        sheets_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        from engine.spec_scheduler import SpecSheet

        s = self._spec_scheduler(sheets_dir)
        path = Path(s.sheets_dir) / (name if name.endswith(".json") else f"{name}.json")
        data: Dict[str, Any] = {"name": name, "jobs": [], "chains": []}
        if path.exists():
            existing = SpecSheet.from_dict(source=str(path), data=json.loads(path.read_text()))
            for spec in existing.jobs.values():
                data["jobs"].append({
                    "name": spec.name, "workflow": spec.workflow, "schedule": spec.schedule,
                    "goal": spec.goal, "provider": spec.provider, "model_name": spec.model_name,
                    "endpoint": spec.endpoint, "require_approval": spec.require_approval,
                    **({"budget": spec.budget} if spec.budget else {}),
                    **({"context": spec.context} if spec.context else {}),
                })
            for chain in existing.chains.values():
                data["chains"].append({"name": chain.name, "jobs": chain.jobs, "fail_strategy": chain.fail_strategy})
        target: Dict[str, Any] = {
            "name": job_name, "workflow": workflow, "schedule": schedule, "goal": goal,
            "provider": provider, "model_name": model_name, "endpoint": endpoint,
            "require_approval": require_approval,
        }
        if context:
            target["context"] = context
        data["jobs"] = [j for j in data["jobs"] if j.get("name") != job_name]
        data["jobs"].append(target)
        SpecSheet.from_dict(source=str(path), data=data)  # validate before writing
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
        return {"sheet": name, "path": str(path), "added_job": job_name, "job_count": len(data["jobs"])}

    def spec_run(
        self,
        sheet: str = "default",
        *,
        job: Optional[str] = None,
        chain: Optional[str] = None,
        sheets_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        s = self._spec_scheduler(sheets_dir)
        if chain:
            outcome = s.run_chain(sheet, chain)
            rows = outcome.to_dict()
            rows["completed"] = bool(outcome.ok)
            return rows
        if not job:
            raise ValueError("spec_run requires a job name or a chain name")
        outcome = s.run_job(sheet, job)
        return _spec_job_outcome(outcome)

    def spec_status(self, *, sheets_dir: Optional[str] = None) -> Dict[str, Any]:
        s = self._spec_scheduler(sheets_dir)
        payload = s.status()
        payload["sheets_dir"] = str(s.sheets_dir)
        return payload


    def observe_run(self, run_id: str) -> Dict[str, Any]:
        return self.service.observe_run(run_id)

    def get_execution_summary(self, execution_id: str) -> Dict[str, Any]:
        return self.service.get_execution_summary(execution_id)

    def get_artifacts(self, execution_id: str) -> list[Dict[str, Any]]:
        return self.service.get_artifacts(execution_id)

    def get_run_artifacts(self, run_id: str) -> list[Dict[str, Any]]:
        return self.service.get_run_artifacts(run_id)

    def list_checkpoints(self) -> list[Dict[str, Any]]:
        return self.service.list_checkpoints()

    def get_checkpoint(self, checkpoint_id: str) -> Optional[Dict[str, Any]]:
        return self.service.get_checkpoint(checkpoint_id)

    def get_change_record(self, checkpoint_id: str) -> Optional[Dict[str, Any]]:
        return self.service.get_change_record(checkpoint_id)

    def rollback_checkpoint(self, checkpoint_id: str, *, actor: str = "runtime-service", reason: str = "rollback requested") -> Dict[str, Any]:
        return self.service.rollback_checkpoint(checkpoint_id, actor=actor, reason=reason)

    def cancel_run(self, run_id: str) -> Dict[str, Any]:
        return self.service.cancel_run(run_id)

    def handle(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        action = str(payload.get("action", "")).strip().lower()
        if action in {"run_task", "task"}:
            return self.run_task(
                payload["task"],
                workspace_path=payload.get("workspace_path"),
                context=payload.get("context"),
            )
        if action == "run_workflow":
            return self.run_workflow(
                payload["workflow_path"],
                provider=payload.get("provider", "stub"),
                model_name=payload.get("model_name"),
                endpoint=payload.get("endpoint"),
                context=payload.get("context"),
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
        if action in {"get_run_progress", "progress"}:
            return self.get_run_progress(payload["run_id"])
        if action in {"observe_run", "observe"}:
            return self.observe_run(payload["run_id"])
        if action in {"get_run_artifacts", "artifacts"}:
            return self.get_run_artifacts(payload["run_id"])
        if action == "list_checkpoints":
            return self.list_checkpoints()
        if action == "get_checkpoint":
            return self.get_checkpoint(payload["checkpoint_id"])
        if action in {"get_change", "get_change_record"}:
            return self.get_change_record(payload["checkpoint_id"])
        if action in {"rollback_checkpoint", "rollback"}:
            return self.rollback_checkpoint(
                payload["checkpoint_id"],
                actor=payload.get("actor", "api-user"),
                reason=payload.get("reason", "rollback requested"),
            )
        if action in {"cancel_run", "cancel"}:
            return self.cancel_run(payload["run_id"])
        if action in {"append_event", "append"}:
            return self.append_event(payload["run_id"], payload.get("event", {}))
        if action in {"finalize_run", "finalize"}:
            return self.finalize_run(payload["run_id"], payload.get("status"))
        if action in {"respond_approval", "approve"}:
            return self.respond_approval(
                payload["run_id"],
                approval_id=payload.get("approval_id"),
                choice=payload.get("choice", "approve"),
            )
        if action in {"approve_tool", "approve_tool_request"}:
            return self.approve_tool_request(
                payload["run_id"],
                payload["approval_id"],
                approved_by=payload.get("approved_by", "api-user"),
                reason=payload.get("reason", "approved"),
                comment=payload.get("comment"),
            )
        if action in {"deny_tool", "deny_tool_request"}:
            return self.deny_tool_request(
                payload["run_id"],
                payload["approval_id"],
                denied_by=payload.get("denied_by", "api-user"),
                reason=payload.get("reason", "denied"),
                comment=payload.get("comment"),
            )
        if action in {"list_tool_approvals", "pending_tool_approvals"}:
            return self.service.list_pending_tool_approvals(payload.get("run_id"))
        if action in {"list_policy_denials", "policy_denials"}:
            return self.service.list_policy_denials(payload.get("run_id"))
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
        # Spec-schedule actions (P3): thin mapping onto the spec_* methods.
        # The sheets_dir is optional; when absent the service's data_dir is
        # used (consistent with the CLI's --data-dir convention).
        if action in {"spec_list", "spec_sheets"}:
            return self.spec_list(sheets_dir=payload.get("sheets_dir"))
        if action in {"spec_show", "spec_get"}:
            return self.spec_show(payload["name"], sheets_dir=payload.get("sheets_dir"))
        if action in {"spec_add", "spec_create_job"}:
            return self.spec_add(
                payload["name"],
                payload["job"],
                payload["workflow"],
                schedule=payload.get("schedule", "manual"),
                goal=payload.get("goal"),
                provider=payload.get("provider"),
                model_name=payload.get("model_name"),
                endpoint=payload.get("endpoint"),
                require_approval=bool(payload.get("require_approval", False)),
                context=payload.get("context"),
                sheets_dir=payload.get("sheets_dir"),
            )
        if action in {"spec_run", "spec_execute", "spec_trigger"}:
            return self.spec_run(
                payload.get("sheet", "default"),
                job=payload.get("job"),
                chain=payload.get("chain"),
                sheets_dir=payload.get("sheets_dir"),
            )
        if action in {"spec_status", "spec_state"}:
            return self.spec_status(sheets_dir=payload.get("sheets_dir"))
        raise ValueError(f"Unsupported action: {action}")

    def append_event(self, run_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        return self.service.append_event(run_id, event)

    def finalize_run(self, run_id: str, status: Optional[str] = None) -> Dict[str, Any]:
        return self.service.finalize_run(run_id, status)

    def respond_approval(self, run_id: str, approval_id: Optional[str] = None, choice: str = "approve") -> Dict[str, Any]:
        return self.service.respond_approval(run_id, approval_id=approval_id, choice=choice)

    def approve_tool_request(
        self,
        run_id: str,
        approval_id: str,
        *,
        approved_by: str = "api-user",
        reason: str = "approved",
        comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.service.approve_tool_request(
            run_id,
            approval_id,
            approved_by=approved_by,
            reason=reason,
            comment=comment,
        )

    def deny_tool_request(
        self,
        run_id: str,
        approval_id: str,
        *,
        denied_by: str = "api-user",
        reason: str = "denied",
        comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.service.deny_tool_request(
            run_id,
            approval_id,
            denied_by=denied_by,
            reason=reason,
            comment=comment,
        )

    def list_pending_tool_approvals(self, run_id: Optional[str] = None) -> list[Dict[str, Any]]:
        return self.service.list_pending_tool_approvals(run_id)

    def list_policy_denials(self, run_id: Optional[str] = None) -> list[Dict[str, Any]]:
        return self.service.list_policy_denials(run_id)

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
