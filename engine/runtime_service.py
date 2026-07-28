from __future__ import annotations

import threading
import uuid
from typing import Any, Dict, List, Optional

from engine.models import ExecutionContext, WorkflowDefinition, _now_iso
from engine.runtime import RuntimeKernel
from engine.storage import SQLiteRunStore
from engine.workflow_loader import build_tool_definitions, build_workflow_definition, load_json_file, resolve_data_paths, resolve_input_path
from workers.local_worker import LocalWorker
from workers.model_adapter import ModelAdapterConfig, ModelAdapterFactory
from engine.models import WorkerRequest


class RuntimeService:
    def __init__(self, data_dir: Optional[str] = None) -> None:
        self.data_dir = data_dir
        self.paths = resolve_data_paths(data_dir)
        self.kernel = RuntimeKernel(self.paths["event_db"], self.paths["artifact_db"], self.paths["workflow_definition_db"])
        self.run_store = SQLiteRunStore(self.paths["event_db"].parent / "runs.db")
        self._background_threads: Dict[str, threading.Thread] = {}
        self._state_lock = threading.RLock()
        self._resume_pending_runs()

    def _update_liveness_and_recovery(self, run_id: str, run_record: Dict[str, Any]) -> None:
        now = _now_iso()
        liveness = dict(run_record.setdefault("liveness", {}))
        liveness["last_heartbeat_at"] = now
        liveness["status"] = run_record.get("status")
        recovery = dict(run_record.setdefault("recovery", {}))
        recovery.setdefault("restart_count", 0)
        run_record["liveness"] = liveness
        run_record["recovery"] = recovery
        run_record["last_updated_at"] = now
        self.run_store.update(run_id, run_record)

    def _load_workflow(self, workflow_path: str) -> WorkflowDefinition:
        resolved_path = resolve_input_path(workflow_path)
        data = load_json_file(resolved_path)
        workflow_definition = build_workflow_definition(data)
        tools = build_tool_definitions(data)

        for tool in tools:
            self.kernel.register_tool(tool)
        self.kernel.register_workflow_definition(workflow_definition)
        return workflow_definition

    def _build_model_adapter(self, provider: str = "stub", model_name: Optional[str] = None, endpoint: Optional[str] = None):
        return ModelAdapterFactory.create(ModelAdapterConfig(provider=provider, model_name=model_name, endpoint=endpoint))

    def _execute_step(self, execution_id: str, step_execution_id: str, model_adapter: Any) -> None:
        execution = self.kernel.step_executions[step_execution_id]
        workflow_definition = self.kernel.workflow_definitions[self.kernel.workflow_executions[execution_id].workflow_definition_id]
        step_definition = workflow_definition.steps[0]
        for candidate in workflow_definition.steps:
            if candidate.id == execution.step_id:
                step_definition = candidate
                break

        decision = self.kernel.assign_execution(
            step_execution_id,
            model_adapter=model_adapter,
            capability=step_definition.role,
            objective=step_definition.objective,
        )
        if not decision.allowed:
            raise RuntimeError(f"Assignment denied: {decision.reason}")

        decision = self.kernel.start_execution(step_execution_id)
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
                self.kernel.request_tool(step_execution_id, recommendation.tool_id, recommendation.action, recommendation.parameters)
        self.kernel.complete_execution(step_execution_id, response.artifacts_created)

    def _advance_workflow(
        self,
        execution_id: str,
        *,
        provider: str = "stub",
        model_name: Optional[str] = None,
        endpoint: Optional[str] = None,
        stop_on_pause: bool = False,
    ) -> None:
        model_adapter = self._build_model_adapter(provider=provider, model_name=model_name, endpoint=endpoint)

        while not self.kernel.workflow_complete(execution_id):
            step_execution_id = self.kernel.schedule_next_step(execution_id)
            if step_execution_id is None:
                if self.kernel.get_workflow_status(execution_id) == "WAITING_APPROVAL":
                    return
                if self.kernel.workflow_complete(execution_id):
                    break
                if stop_on_pause:
                    return
                raise RuntimeError("Workflow is not complete and no ready steps remain")

            self._execute_step(execution_id, step_execution_id, model_adapter)
            if stop_on_pause and self.kernel.get_workflow_status(execution_id) in {"WAITING_APPROVAL", "COMPLETED", "FAILED"}:
                break

        if self.kernel.workflow_complete(execution_id):
            self.kernel.complete_workflow(execution_id)

    def _build_run_payload(self, run_id: str, execution_id: str, workflow_name: str, workflow_path: str, status: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        workflow_execution = self.kernel.workflow_executions[execution_id]
        normalized_context = dict(context or {})
        return {
            "run_id": run_id,
            "execution_id": execution_id,
            "workflow_name": workflow_name,
            "workflow_path": workflow_path,
            "status": status,
            "started_at": workflow_execution.started_at,
            "completed_at": workflow_execution.completed_at,
            "active_controls": [],
            "pending_approval_id": None,
            "last_event_id": workflow_execution.events[-1] if workflow_execution.events else None,
            "completed_steps": [],
            "artifacts": [],
            "artifact_ids": [],
            "context": normalized_context,
            "events": [],
            "last_updated_at": _now_iso(),
        }

    def _find_existing_run(self, context: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not context:
            return None
        idempotency_key = context.get("idempotency_key") or context.get("run_idempotency_key")
        if not idempotency_key:
            return None
        for run_record in self.run_store.list():
            if run_record.get("idempotency_key") == idempotency_key:
                return run_record
        return None

    def _record_lifecycle_event_if_needed(self, run_id: str, previous_status: Optional[str], next_status: str) -> None:
        if previous_status == next_status:
            return
        if previous_status in {None, "STARTING", "QUEUED"} and next_status in {"RUNNING", "WAITING_APPROVAL", "COMPLETED", "FAILED", "CANCELLED"}:
            self._record_run_event(run_id, "RUN_STARTED", {"status": next_status})
        if previous_status in {None, "STARTING", "RUNNING"} and next_status == "WAITING_APPROVAL":
            self._record_run_event(run_id, "RUN_PAUSED", {"status": next_status})
        elif previous_status == "WAITING_APPROVAL" and next_status not in {"WAITING_APPROVAL", "COMPLETED", "FAILED", "CANCELLED"}:
            self._record_run_event(run_id, "RUN_RESUMED", {"status": next_status})
        elif next_status in {"COMPLETED", "FAILED", "CANCELLED"}:
            self._record_run_event(run_id, "RUN_FINALIZED", {"status": next_status})

    def _sync_run_state(self, run_id: str) -> Dict[str, Any]:
        with self._state_lock:
            run_record = self.run_store.get(run_id)
            if run_record is None:
                raise KeyError(f"Run '{run_id}' not found")

            if run_record.get("status") == "CANCELLED":
                run_record["active_controls"] = []
                self.run_store.update(run_id, run_record)
                return run_record

            execution_id = run_record["execution_id"]
            workflow_execution = self.kernel.workflow_executions.get(execution_id)
            if workflow_execution is None:
                return run_record

            workflow_definition = self.kernel.workflow_definitions.get(workflow_execution.workflow_definition_id)
            completed_steps = [self.kernel.step_executions[step_id].step_id for step_id in workflow_execution.completed_executions]
            artifacts = list(workflow_execution.produced_artifacts)
            previous_artifact_ids = set(run_record.get("artifact_ids", []))
            active_controls = []
            pending_approval_id = None
            if workflow_execution.status == "WAITING_APPROVAL":
                active_controls = ["respond_approval"]
                for event_id in reversed(workflow_execution.events):
                    event = self.kernel.event_log.get(event_id)
                    if event is not None and event.event_type == "APPROVAL_REQUIRED":
                        pending_approval_id = event.event_id
                        break
            elif workflow_execution.status in {"COMPLETED", "FAILED"}:
                active_controls = []
            else:
                active_controls = ["observe_run"]

            run_status = run_record.get("status")
            if run_status in {"COMPLETED", "FAILED", "CANCELLED"}:
                status = run_status
            elif run_record.get("queued_for_background") and run_status == "QUEUED" and workflow_execution.status in {"PLANNING", "CREATED"}:
                status = "QUEUED"
            else:
                status = workflow_execution.status

            previous_status = run_record.get("status")
            run_record.update({
                "workflow_name": workflow_definition.name if workflow_definition is not None else run_record.get("workflow_name", "<unknown>"),
                "status": status,
                "started_at": workflow_execution.started_at,
                "completed_at": workflow_execution.completed_at,
                "active_controls": active_controls if status not in {"COMPLETED", "FAILED", "CANCELLED"} else [],
                "pending_approval_id": pending_approval_id,
                "last_event_id": run_record.get("last_event_id") or (workflow_execution.events[-1] if workflow_execution.events else None),
                "completed_steps": completed_steps,
                "artifacts": artifacts,
                "context": run_record.get("context", {}),
            })
            new_artifact_ids = [artifact_id for artifact_id in artifacts if artifact_id not in previous_artifact_ids]
            for artifact_id in new_artifact_ids:
                self._record_run_event(run_id, "ARTIFACT_ADDED", {"artifact_id": artifact_id})

            self._record_lifecycle_event_if_needed(run_id, previous_status, status)
            self._update_liveness_and_recovery(run_id, run_record)
            run_record["artifact_ids"] = artifacts
            self.run_store.update(run_id, run_record)
            return run_record

    def _run_workflow_in_background(self, run_id: str, execution_id: str, *, provider: str = "stub", model_name: Optional[str] = None, endpoint: Optional[str] = None) -> None:
        with self._state_lock:
            try:
                self._sync_run_state(run_id)
                self._advance_workflow(execution_id, provider=provider, model_name=model_name, endpoint=endpoint)
            except Exception as exc:
                run_record = self.run_store.get(run_id)
                if run_record is not None:
                    run_record["status"] = "FAILED"
                    run_record["error"] = str(exc)
                    self.run_store.update(run_id, run_record)
            finally:
                try:
                    self._sync_run_state(run_id)
                except Exception:
                    pass

    def run_workflow(
        self,
        workflow_path: str,
        *,
        provider: str = "stub",
        model_name: Optional[str] = None,
        endpoint: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._state_lock:
            workflow_definition = self._load_workflow(workflow_path)
            execution_context = ExecutionContext.from_dict(context) if context else ExecutionContext.default()
            execution_id = self.kernel.start_workflow(workflow_definition.workflow_definition_id, execution_context)
            self._advance_workflow(execution_id, provider=provider, model_name=model_name, endpoint=endpoint)
            return self.get_execution_summary(execution_id)

    def start_run(
        self,
        workflow_path: str,
        *,
        provider: str = "stub",
        model_name: Optional[str] = None,
        endpoint: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._state_lock:
            existing_run = self._find_existing_run(context)
            if existing_run is not None:
                return self.get_run(existing_run["run_id"])

            workflow_definition = self._load_workflow(workflow_path)
            execution_context = ExecutionContext.from_dict(context) if context else ExecutionContext.default()
            execution_id = self.kernel.start_workflow(workflow_definition.workflow_definition_id, execution_context)
            run_id = str(uuid.uuid4())
            run_record = self._build_run_payload(run_id, execution_id, workflow_definition.name, workflow_path, "STARTING", context=context)
            run_record["idempotency_key"] = context.get("idempotency_key") if context else None
            if context and context.get("auto_approve"):
                run_record["auto_approve"] = True
            if context:
                for key in ["session_id", "agent_goal", "workflow_template", "runtime_hints", "user_intent_summary", "last_user_message"]:
                    if key in context:
                        run_record["context"][key] = context[key]
            self.run_store.create(run_id, run_record)
            self._record_run_event(run_id, "RUN_CREATED", {"workflow_path": workflow_path, "workflow_name": workflow_definition.name, "execution_id": execution_id}, source="bridge")
            self._advance_workflow(execution_id, provider=provider, model_name=model_name, endpoint=endpoint, stop_on_pause=True)
            self._sync_run_state(run_id)
            if self._should_auto_approve(run_record):
                self._auto_approve_if_needed(run_id, execution_id)
            self._update_liveness_and_recovery(run_id, run_record)
            thread = threading.Thread(
                target=self._run_workflow_in_background,
                args=(run_id, execution_id),
                kwargs={"provider": provider, "model_name": model_name, "endpoint": endpoint},
                daemon=True,
            )
            self._background_threads[run_id] = thread
            thread.start()
            return self._sync_run_state(run_id)

    def queue_run(
        self,
        workflow_path: str,
        *,
        provider: str = "stub",
        model_name: Optional[str] = None,
        endpoint: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._state_lock:
            existing_run = self._find_existing_run(context)
            if existing_run is not None:
                return self.get_run(existing_run["run_id"])

            workflow_definition = self._load_workflow(workflow_path)
            execution_context = ExecutionContext.from_dict(context) if context else ExecutionContext.default()
            execution_id = self.kernel.start_workflow(workflow_definition.workflow_definition_id, execution_context)
            run_id = str(uuid.uuid4())
            run_record = self._build_run_payload(run_id, execution_id, workflow_definition.name, workflow_path, "QUEUED", context=context)
            run_record["idempotency_key"] = context.get("idempotency_key") if context else None
            if context and context.get("auto_approve"):
                run_record["auto_approve"] = True
            if context:
                for key in ["session_id", "agent_goal", "workflow_template", "runtime_hints", "user_intent_summary", "last_user_message"]:
                    if key in context:
                        run_record["context"][key] = context[key]
            run_record["queued_for_background"] = True
            self.run_store.create(run_id, run_record)
            self._record_run_event(run_id, "RUN_QUEUED", {"workflow_path": workflow_path, "workflow_name": workflow_definition.name, "execution_id": execution_id}, source="bridge")
            self._update_liveness_and_recovery(run_id, run_record)
            thread = threading.Thread(
                target=self._run_queued_workflow,
                args=(run_id, execution_id),
                kwargs={"provider": provider, "model_name": model_name, "endpoint": endpoint},
                daemon=True,
            )
            self._background_threads[run_id] = thread
            thread.start()
            return {
                "run_id": run_id,
                "execution_id": execution_id,
                "workflow_name": workflow_definition.name,
                "workflow_path": workflow_path,
                "status": "QUEUED",
                "context": context or {},
            }

    def _record_run_event(
        self,
        run_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        actor: Optional[str] = None,
        correlation_id: Optional[str] = None,
        status: Optional[str] = None,
        source: str = "bridge",
    ) -> Optional[Dict[str, Any]]:
        run_record = self.run_store.get(run_id)
        if run_record is None:
            return None

        event_id = str(uuid.uuid4())
        event_payload = dict(payload or {})
        if actor is not None:
            event_payload["actor"] = actor
        if correlation_id is not None:
            event_payload["correlation_id"] = correlation_id
        event_payload.setdefault("run_id", run_id)
        event_payload.setdefault("execution_id", run_record.get("execution_id"))
        event_payload.setdefault("source", source)
        event_payload.setdefault("status_after", status or run_record.get("status"))
        event_record = {
            "event_id": event_id,
            "event_type": event_type,
            "timestamp": _now_iso(),
            "payload": event_payload,
        }
        run_record.setdefault("events", []).append(event_record)
        run_record["last_event_id"] = event_id
        run_record["last_updated_at"] = event_record["timestamp"]
        if status is not None:
            run_record["status"] = status
        self.run_store.update(run_id, run_record)
        return event_record

    def _resume_pending_runs(self) -> None:
        for run_record in self.run_store.list():
            run_id = run_record.get("run_id")
            if not run_id:
                continue
            if not run_record.get("queued_for_background"):
                continue
            if run_record.get("status") in {"COMPLETED", "FAILED", "CANCELLED"}:
                continue
            execution_id = run_record.get("execution_id")
            if not execution_id:
                continue
            workflow_status = self.kernel.get_workflow_status(execution_id)
            if workflow_status in {"COMPLETED", "FAILED", "CANCELLED"}:
                continue
            recovery = dict(run_record.setdefault("recovery", {}))
            recovery["restart_count"] = recovery.get("restart_count", 0) + 1
            run_record["recovery"] = recovery
            self.run_store.update(run_id, run_record)
            thread = threading.Thread(
                target=self._run_queued_workflow,
                args=(run_id, execution_id),
                daemon=True,
            )
            self._background_threads[run_id] = thread
            thread.start()

    def _should_auto_approve(self, run_record: Dict[str, Any]) -> bool:
        return bool(run_record.get("auto_approve") or run_record.get("context", {}).get("auto_approve"))

    def _auto_approve_if_needed(self, run_id: str, execution_id: str) -> None:
        run_record = self.run_store.get(run_id)
        if run_record is None:
            return
        if self.kernel.get_workflow_status(execution_id) != "WAITING_APPROVAL":
            return
        try:
            self.kernel.approve_workflow(execution_id, approved_by="auto-approval", reason="auto-approved for unattended execution")
            self._record_run_event(run_id, "CONTROL_RESPONDED", {"choice": "approve", "mode": "auto"}, status="RUNNING", source="bridge")
            self._advance_workflow(execution_id)
        except Exception:
            pass

    def _run_queued_workflow(self, run_id: str, execution_id: str, *, provider: str = "stub", model_name: Optional[str] = None, endpoint: Optional[str] = None) -> None:
        try:
            self._sync_run_state(run_id)
            self._advance_workflow(execution_id, provider=provider, model_name=model_name, endpoint=endpoint, stop_on_pause=True)
            if self._should_auto_approve(self.run_store.get(run_id) or {}):
                self._auto_approve_if_needed(run_id, execution_id)
            self._sync_run_state(run_id)
        except Exception as exc:
            run_record = self.run_store.get(run_id)
            if run_record is not None:
                run_record["status"] = "FAILED"
                run_record["error"] = str(exc)
                self.run_store.update(run_id, run_record)
        finally:
            try:
                self._sync_run_state(run_id)
            except Exception:
                pass

    def get_execution_summary(self, execution_id: str) -> Dict[str, Any]:
        workflow_execution = self.kernel.workflow_executions[execution_id]
        workflow_definition = self.kernel.workflow_definitions.get(workflow_execution.workflow_definition_id)
        completed_steps = [self.kernel.step_executions[step_id].step_id for step_id in workflow_execution.completed_executions]
        artifacts = list(workflow_execution.produced_artifacts)
        return {
            "execution_id": workflow_execution.execution_id,
            "workflow_name": workflow_definition.name if workflow_definition is not None else "<unknown>",
            "status": workflow_execution.status,
            "started_at": workflow_execution.started_at,
            "completed_at": workflow_execution.completed_at,
            "completed_steps": completed_steps,
            "artifacts": artifacts,
        }

    def get_run(self, run_id: str) -> Dict[str, Any]:
        with self._state_lock:
            run_record = self._sync_run_state(run_id)
            return {
                "run_id": run_record["run_id"],
                "execution_id": run_record["execution_id"],
                "workflow_name": run_record["workflow_name"],
                "workflow_path": run_record.get("workflow_path"),
                "status": run_record["status"],
                "started_at": run_record["started_at"],
                "completed_at": run_record["completed_at"],
                "completed_steps": run_record["completed_steps"],
                "artifacts": run_record["artifacts"],
                "active_controls": run_record.get("active_controls", []),
                "pending_approval_id": run_record.get("pending_approval_id"),
                "last_event_id": run_record.get("last_event_id"),
                "context": run_record.get("context", {}),
                "error": run_record.get("error"),
                "liveness": run_record.get("liveness", {}),
                "recovery": run_record.get("recovery", {}),
            }

    def observe_run(self, run_id: str) -> Dict[str, Any]:
        with self._state_lock:
            run_record = self._sync_run_state(run_id)
            workflow_execution = self.kernel.workflow_executions[run_record["execution_id"]]
            events = []
            for event_id in workflow_execution.events:
                event = self.kernel.event_log.get(event_id)
                if event is not None:
                    events.append({
                        "event_id": event.event_id,
                        "event_type": event.event_type,
                        "payload": event.payload,
                    })
            for event in run_record.get("events", []):
                if not any(existing.get("event_id") == event.get("event_id") for existing in events):
                    payload = dict(event.get("payload", {}))
                    event_source = event.get("source", payload.get("source", "bridge"))
                    event_status_after = event.get("status_after", payload.get("status_after"))
                    events.append({
                        "event_id": event.get("event_id"),
                        "event_type": event.get("event_type"),
                        "payload": payload,
                        "source": event_source,
                        "status_after": event_status_after,
                    })
            if not any(event.get("source") == "bridge" for event in events):
                for event in events:
                    if isinstance(event.get("payload"), dict):
                        event.setdefault("source", event["payload"].get("source", "bridge"))
                        event.setdefault("status_after", event["payload"].get("status_after"))
            return {
                "run_id": run_id,
                "status": run_record["status"],
                "events": events,
                "cursor": str(len(events)),
            }

    def get_artifacts(self, execution_id: str) -> List[Dict[str, Any]]:
        workflow_execution = self.kernel.workflow_executions[execution_id]
        artifacts = []
        for artifact_id in workflow_execution.produced_artifacts:
            artifact = self.kernel.artifact_store.get(artifact_id)
            if artifact is not None:
                artifacts.append({
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "title": artifact.title,
                    "version": artifact.version,
                    "status": artifact.status,
                })
        return artifacts

    def get_run_artifacts(self, run_id: str) -> List[Dict[str, Any]]:
        with self._state_lock:
            run_record = self.run_store.get(run_id)
            if run_record is None:
                raise KeyError(f"Run '{run_id}' not found")
            return self.get_artifacts(run_record["execution_id"])

    def append_event(self, run_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        with self._state_lock:
            run_record = self.run_store.get(run_id)
            if run_record is None:
                raise KeyError(f"Run '{run_id}' not found")

            event_id = event.get("event_id") or str(uuid.uuid4())
            normalized_event = {"event_id": event_id, **event}
            normalized_event.setdefault("timestamp", _now_iso())

            payload = normalized_event.get("payload") or {}
            payload = dict(payload) if isinstance(payload, dict) else {"value": payload}
            payload.setdefault("source", "bridge")
            payload.setdefault("status_after", run_record.get("status"))
            normalized_event["payload"] = payload
            normalized_event.setdefault("source", payload["source"])
            normalized_event.setdefault("status_after", payload["status_after"])

            run_record.setdefault("events", []).append(normalized_event)
            run_record["last_event_id"] = event_id
            run_record["last_updated_at"] = normalized_event["timestamp"]
            self.run_store.update(run_id, run_record)
            return {"accepted": True, "status": "appended", "run_id": run_id, "event_id": event_id}

    def handle_action(
        self,
        run_id: str,
        action: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        actor: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._state_lock:
            run_record = self.run_store.get(run_id)
            if run_record is None:
                raise KeyError(f"Run '{run_id}' not found")

            action_name = str(action or "").strip().lower()
            payload = payload or {}
            applied_controls = run_record.setdefault("applied_controls", [])
            if correlation_id is not None and any(item.get("correlation_id") == correlation_id and item.get("action") == action_name for item in applied_controls):
                return {"accepted": False, "status": "duplicate", "run_id": run_id, "message": "control action already applied"}

            if action_name in {"queue_message", "message", "enqueue_message"}:
                message_text = payload.get("text") or payload.get("message") or ""
                event_payload = {"text": message_text, "actor": actor, "correlation_id": correlation_id}
                result = self.append_event(run_id, {"event_type": "MESSAGE_ENQUEUED", "payload": event_payload})
                if result.get("accepted"):
                    run_record = self.run_store.get(run_id)
                    if run_record is None:
                        raise KeyError(f"Run '{run_id}' not found")
                    applied_controls = run_record.setdefault("applied_controls", [])
                    applied_controls.append({"action": action_name, "correlation_id": correlation_id, "actor": actor})
                    run_record["applied_controls"] = applied_controls
                    self.run_store.update(run_id, run_record)
                return result

            if action_name in {"update_goal", "set_goal"}:
                goal_value = payload.get("goal") or payload.get("text") or payload.get("value")
                if goal_value is None:
                    return {"accepted": False, "status": "invalid", "run_id": run_id, "message": "goal payload is required"}
                context = run_record.setdefault("context", {})
                agent_context = context.setdefault("agent_context", {})
                agent_context["goal"] = goal_value
                context["agent_context"] = agent_context
                run_record["context"] = context
                run_record["goal"] = goal_value
                self.run_store.update(run_id, run_record)
                self._record_run_event(run_id, "GOAL_UPDATED", {"goal": goal_value}, actor=actor, correlation_id=correlation_id)
                run_record = self.run_store.get(run_id)
                if run_record is None:
                    raise KeyError(f"Run '{run_id}' not found")
                applied_controls = run_record.setdefault("applied_controls", [])
                applied_controls.append({"action": action_name, "correlation_id": correlation_id, "actor": actor})
                run_record["applied_controls"] = applied_controls
                self.run_store.update(run_id, run_record)
                return {"accepted": True, "status": "updated", "run_id": run_id, "message": "goal updated"}

            if action_name in {"clarify_request", "clarify"}:
                event_payload = {"request": payload.get("request") or payload.get("text") or "", "actor": actor, "correlation_id": correlation_id}
                result = self.append_event(run_id, {"event_type": "CONTROL_REQUESTED", "payload": event_payload})
                if result.get("accepted"):
                    run_record = self.run_store.get(run_id)
                    if run_record is None:
                        raise KeyError(f"Run '{run_id}' not found")
                    applied_controls = run_record.setdefault("applied_controls", [])
                    applied_controls.append({"action": action_name, "correlation_id": correlation_id, "actor": actor})
                    run_record["applied_controls"] = applied_controls
                    self.run_store.update(run_id, run_record)
                return result

            if action_name in {"append_event", "append"}:
                event_data = payload.get("event", payload)
                result = self.append_event(run_id, event_data)
                if result.get("accepted"):
                    run_record = self.run_store.get(run_id)
                    if run_record is None:
                        raise KeyError(f"Run '{run_id}' not found")
                    applied_controls = run_record.setdefault("applied_controls", [])
                    applied_controls.append({"action": action_name, "correlation_id": correlation_id, "actor": actor})
                    run_record["applied_controls"] = applied_controls
                    self.run_store.update(run_id, run_record)
                return result

            if action_name in {"finalize_run", "finalize"}:
                result = self.finalize_run(run_id, payload.get("status"))
                if result.get("accepted"):
                    run_record = self.run_store.get(run_id)
                    if run_record is None:
                        raise KeyError(f"Run '{run_id}' not found")
                    applied_controls = run_record.setdefault("applied_controls", [])
                    applied_controls.append({"action": action_name, "correlation_id": correlation_id, "actor": actor})
                    run_record["applied_controls"] = applied_controls
                    self.run_store.update(run_id, run_record)
                return result

            if action_name in {"cancel_run", "cancel"}:
                result = self.cancel_run(run_id)
                if result.get("accepted"):
                    run_record = self.run_store.get(run_id)
                    if run_record is None:
                        raise KeyError(f"Run '{run_id}' not found")
                    applied_controls = run_record.setdefault("applied_controls", [])
                    applied_controls.append({"action": action_name, "correlation_id": correlation_id, "actor": actor})
                    run_record["applied_controls"] = applied_controls
                    self.run_store.update(run_id, run_record)
                return result

            if action_name in {"respond_approval", "approve"}:
                result = self.respond_approval(run_id, str(payload.get("choice", "approve")))
                if result.get("accepted"):
                    run_record = self.run_store.get(run_id)
                    if run_record is None:
                        raise KeyError(f"Run '{run_id}' not found")
                    applied_controls = run_record.setdefault("applied_controls", [])
                    applied_controls.append({"action": action_name, "correlation_id": correlation_id, "actor": actor})
                    run_record["applied_controls"] = applied_controls
                    self.run_store.update(run_id, run_record)
                return result

            return {"accepted": False, "status": "unsupported", "run_id": run_id, "message": f"unsupported control action: {action}"}

    def finalize_run(self, run_id: str, status: Optional[str] = None) -> Dict[str, Any]:
        with self._state_lock:
            run_record = self.run_store.get(run_id)
            if run_record is None:
                raise KeyError(f"Run '{run_id}' not found")

            next_status = str(status or "COMPLETED").upper()
            run_record["status"] = next_status
            run_record["completed_at"] = None
            run_record["active_controls"] = []
            run_record["error"] = None
            self.run_store.update(run_id, run_record)
            self._record_run_event(run_id, "RUN_FINALIZED", {"status": next_status}, status=next_status, source="bridge")
            return {"accepted": True, "status": next_status, "run_id": run_id, "message": "run finalized"}

    def cancel_run(self, run_id: str) -> Dict[str, Any]:
        with self._state_lock:
            run_record = self.run_store.get(run_id)
            if run_record is None:
                raise KeyError(f"Run '{run_id}' not found")

            execution_id = run_record["execution_id"]
            workflow_status = self.kernel.get_workflow_status(execution_id)
            if workflow_status in {"COMPLETED", "FAILED", "CANCELLED"}:
                return {"accepted": False, "status": workflow_status, "run_id": run_id, "message": "workflow is already terminal"}

            run_record["status"] = "CANCELLED"
            run_record["completed_at"] = None
            run_record["active_controls"] = []
            run_record["error"] = "cancelled by user"
            self.run_store.update(run_id, run_record)
            self._record_run_event(run_id, "RUN_FINALIZED", {"status": "CANCELLED"}, status="CANCELLED", source="bridge")
            return {"accepted": True, "status": "CANCELLED", "run_id": run_id, "message": "run cancelled"}

    def respond_approval(self, run_id: str, choice: str) -> Dict[str, Any]:
        with self._state_lock:
            summary = self.get_run(run_id)
            if summary["status"] != "WAITING_APPROVAL":
                return {
                    "accepted": False,
                    "status": summary["status"],
                    "message": "workflow is not waiting for approval",
                }

            normalized_choice = str(choice or "").strip().lower()
            if normalized_choice != "approve":
                return {
                    "accepted": False,
                    "status": summary["status"],
                    "message": "unsupported approval choice",
                }

            execution_id = summary["execution_id"]
            self.kernel.approve_workflow(execution_id, approved_by="ui-user", reason="approved", comment=None)
            self._record_run_event(run_id, "CONTROL_RESPONDED", {"choice": "approve"}, status="RUNNING", source="bridge")
            self._advance_workflow(execution_id)
            updated = self.get_run(run_id)
            return {
                "accepted": True,
                "status": updated["status"],
                "run_id": run_id,
                "message": "approval recorded",
            }

    def __enter__(self) -> "RuntimeService":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.shutdown()

    def shutdown(self) -> None:
        for thread in list(self._background_threads.values()):
            if thread.is_alive():
                thread.join(timeout=0.1)
        self.run_store.close()
        self.kernel.shutdown()
