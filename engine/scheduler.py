from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - platform fallback
    fcntl = None

from .models import StepDefinition, StepExecution, WorkflowDefinition, WorkflowExecution


class SQLiteJobStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                task TEXT NOT NULL,
                schedule TEXT NOT NULL,
                runtime_budget_seconds INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_run_at TEXT,
                payload TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    def create(self, job_id: str, task: str, schedule: str, runtime_budget_seconds: int, status: str, created_at: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.connection.execute(
            "INSERT INTO jobs (job_id, task, schedule, runtime_budget_seconds, status, created_at, last_run_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, task, schedule, runtime_budget_seconds, status, created_at, None, json.dumps(payload, sort_keys=True)),
        )
        self.connection.commit()
        return self.get(job_id)

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        row = self.connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return {
            "job_id": row["job_id"],
            "task": row["task"],
            "schedule": row["schedule"],
            "runtime_budget_seconds": row["runtime_budget_seconds"],
            "status": row["status"],
            "created_at": row["created_at"],
            "last_run_at": row["last_run_at"],
            "payload": json.loads(row["payload"]),
        }

    def update(self, job_id: str, **updates: Any) -> Dict[str, Any]:
        if updates:
            fields = []
            values: List[Any] = []
            for key, value in updates.items():
                if key == "payload":
                    value = json.dumps(value, sort_keys=True)
                fields.append(f"{key} = ?")
                values.append(value)
            values.append(job_id)
            self.connection.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE job_id = ?", values)
            self.connection.commit()
        return self.get(job_id)

    def list_due(self, now: datetime) -> List[Dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM jobs WHERE status = 'pending' ORDER BY created_at ASC").fetchall()
        due_jobs: List[Dict[str, Any]] = []
        for row in rows:
            schedule = row["schedule"]
            if schedule == "manual":
                continue
            if schedule.startswith("interval:"):
                interval_seconds = int(schedule.split(":", 1)[1])
                created_at = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                if now - created_at >= timedelta(seconds=interval_seconds):
                    due_jobs.append(self.get(row["job_id"]))
            else:
                due_jobs.append(self.get(row["job_id"]))
        return due_jobs

    def close(self) -> None:
        self.connection.close()


class LocalLeaseLockBackend:
    def __init__(self, root_path: Path) -> None:
        self.root_path = Path(root_path)
        self.root_path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _parse_iso(value: Optional[str]) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc)
        if isinstance(value, str):
            candidate = value.strip()
            if not candidate:
                return None
            if candidate.endswith("Z"):
                candidate = candidate[:-1] + "+00:00"
            try:
                return datetime.fromisoformat(candidate).astimezone(timezone.utc)
            except ValueError:
                return None
        return None

    def _lock_path(self, job_id: str) -> Path:
        return self.root_path / f".hermes-job-{job_id}.lock"

    def _read_state(self, job_id: str) -> Optional[Dict[str, Any]]:
        path = self._lock_path(job_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def _write_state(self, job_id: str, payload: Dict[str, Any]) -> None:
        path = self._lock_path(job_id)
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    def acquire(self, job_id: str, owner_id: str, lease_seconds: int = 60) -> Optional[Dict[str, Any]]:
        now = self._utc_now()
        current = self._read_state(job_id)
        if isinstance(current, dict):
            expires_at = self._parse_iso(current.get("expires_at"))
            if expires_at is not None and expires_at > now:
                if current.get("owner_id") == owner_id:
                    renewed = dict(current)
                    renewed["acquired_at"] = now.isoformat().replace("+00:00", "Z")
                    renewed["lease_seconds"] = max(int(lease_seconds), 1)
                    renewed["expires_at"] = (now + timedelta(seconds=max(int(lease_seconds), 1))).isoformat().replace("+00:00", "Z")
                    self._write_state(job_id, renewed)
                    return renewed
                return None

        state = {
            "job_id": job_id,
            "owner_id": owner_id,
            "acquired_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": (now + timedelta(seconds=max(int(lease_seconds), 1))).isoformat().replace("+00:00", "Z"),
            "lease_seconds": max(int(lease_seconds), 1),
        }
        self._write_state(job_id, state)
        return state

    def renew(self, job_id: str, owner_id: str, lease_seconds: int = 60) -> Optional[Dict[str, Any]]:
        return self.acquire(job_id, owner_id, lease_seconds=lease_seconds)

    def release(self, job_id: str, owner_id: str) -> bool:
        current = self._read_state(job_id)
        if not isinstance(current, dict):
            return True
        if current.get("owner_id") != owner_id:
            return False
        path = self._lock_path(job_id)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return False
        return True


class SQLiteLeaseLockBackend:
    """A simple SQLite-backed lease lock backend suitable for cross-process locks."""
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS locks (
                job_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                lease_seconds INTEGER NOT NULL
            )
            """
        )
        self.conn.commit()

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    def _parse_iso(self, value: Optional[str]) -> Optional[datetime]:
        if value is None:
            return None
        try:
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            return datetime.fromisoformat(value).astimezone(timezone.utc)
        except Exception:
            return None

    def acquire(self, job_id: str, owner_id: str, lease_seconds: int = 60) -> Optional[Dict[str, Any]]:
        now = self._utc_now()
        cur = self.conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute("SELECT owner_id, expires_at FROM locks WHERE job_id = ?", (job_id,)).fetchone()
            reclaimed = False
            if row:
                expires_at = self._parse_iso(row["expires_at"])
                if expires_at and expires_at > now:
                    # lock held by someone else
                    self.conn.rollback()
                    return None
                # row existed but was expired or unparsable -> we'll reclaim it
                reclaimed = True
            acquired_at = now.isoformat().replace("+00:00", "Z")
            expires_at = (now + timedelta(seconds=max(1, int(lease_seconds)))).isoformat().replace("+00:00", "Z")
            cur.execute(
                "INSERT OR REPLACE INTO locks (job_id, owner_id, acquired_at, expires_at, lease_seconds) VALUES (?, ?, ?, ?, ?)",
                (job_id, owner_id, acquired_at, expires_at, int(lease_seconds)),
            )
            self.conn.commit()
            result = {"job_id": job_id, "owner_id": owner_id, "acquired_at": acquired_at, "expires_at": expires_at, "lease_seconds": int(lease_seconds)}
            if reclaimed:
                result["reclaimed"] = True
            return result
        except sqlite3.OperationalError:
            try:
                self.conn.rollback()
            except Exception:
                pass
            return None

    def safe_claim(self, job_id: str, owner_id: str, lease_seconds: int = 60, reclaim_grace_seconds: int = 1) -> Optional[Dict[str, Any]]:
        """Attempt to claim an expired lock only if it expired sufficiently long ago.

        This avoids immediately reclaiming a recently-expired lease held by a process
        that may be slow to renew.
        """
        now = self._utc_now()
        cur = self.conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute("SELECT owner_id, expires_at FROM locks WHERE job_id = ?", (job_id,)).fetchone()
            if row:
                expires_at = None
                try:
                    candidate = row["expires_at"]
                    if candidate and candidate.endswith("Z"):
                        candidate = candidate[:-1] + "+00:00"
                    expires_at = datetime.fromisoformat(candidate).astimezone(timezone.utc) if candidate else None
                except Exception:
                    expires_at = None
                if expires_at is not None and expires_at + timedelta(seconds=reclaim_grace_seconds) > now:
                    # not expired long enough
                    self.conn.rollback()
                    return None
            acquired_at = now.isoformat().replace("+00:00", "Z")
            expires_at = (now + timedelta(seconds=max(1, int(lease_seconds)))).isoformat().replace("+00:00", "Z")
            cur.execute(
                "INSERT OR REPLACE INTO locks (job_id, owner_id, acquired_at, expires_at, lease_seconds) VALUES (?, ?, ?, ?, ?)",
                (job_id, owner_id, acquired_at, expires_at, int(lease_seconds)),
            )
            self.conn.commit()
            return {"job_id": job_id, "owner_id": owner_id, "acquired_at": acquired_at, "expires_at": expires_at, "lease_seconds": int(lease_seconds), "reclaimed": True}
        except sqlite3.OperationalError:
            try:
                self.conn.rollback()
            except Exception:
                pass
            return None

    def renew(self, job_id: str, owner_id: str, lease_seconds: int = 60) -> Optional[Dict[str, Any]]:
        # reuse acquire semantics for renew
        return self.acquire(job_id, owner_id, lease_seconds=lease_seconds)

    def release(self, job_id: str, owner_id: str) -> bool:
        cur = self.conn.cursor()
        row = cur.execute("SELECT owner_id FROM locks WHERE job_id = ?", (job_id,)).fetchone()
        if not row:
            return True
        if row["owner_id"] != owner_id:
            return False
        cur.execute("DELETE FROM locks WHERE job_id = ?", (job_id,))
        self.conn.commit()
        return True

    def list_locks(self) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        rows = cur.execute("SELECT job_id, owner_id, acquired_at, expires_at, lease_seconds FROM locks").fetchall()
        return [{"job_id": r["job_id"], "owner_id": r["owner_id"], "acquired_at": r["acquired_at"], "expires_at": r["expires_at"], "lease_seconds": r["lease_seconds"]} for r in rows]

    def force_release(self, job_id: str) -> bool:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM locks WHERE job_id = ?", (job_id,))
        self.conn.commit()
        return True


class BackgroundScheduler:
    def __init__(self, job_store: SQLiteJobStore, lock_backend: Optional[Any] = None) -> None:
        self.job_store = job_store
        self.lock_backend = lock_backend or LocalLeaseLockBackend(job_store.path.parent)
        self._active_jobs: set[str] = set()
        self._active_jobs_lock = threading.RLock()

    def _acquire_process_job_lock(self, job_id: str, owner_id: str, lease_seconds: int) -> Optional[Dict[str, Any]]:
        if fcntl is not None:
            lock_path = self.job_store.path.parent / f".hermes-job-{job_id}.lock"
            handle = lock_path.open("a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                return None
            lease = self.lock_backend.acquire(job_id, owner_id, lease_seconds=lease_seconds)
            if lease is None:
                # underlying backend refused; release file lock and fail acquisition
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    handle.close()
                except Exception:
                    pass
                return None
            return {"handle": handle, "owner_id": owner_id, "lease": lease}
        return {"handle": True, "owner_id": owner_id, "lease": self.lock_backend.acquire(job_id, owner_id, lease_seconds=lease_seconds)}

    @staticmethod
    def _release_process_job_lock(handle) -> None:
        if handle is True or handle is None:
            return
        if hasattr(handle, "get"):
            inner_handle = handle.get("handle")
            if inner_handle is not None and inner_handle is not True:
                if fcntl is not None:
                    fcntl.flock(inner_handle.fileno(), fcntl.LOCK_UN)
                inner_handle.close()
            return
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    @staticmethod
    def _normalize_policy_context(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        context = dict(payload or {})
        policy_context = context.get("policy_context")
        if policy_context is None:
            context["policy_context"] = {"approved": True, "policy_ids": []}
        elif not isinstance(policy_context, dict):
            context["policy_context"] = {"approved": False, "policy_ids": [], "reason": "policy_context.invalid"}
        else:
            normalized = dict(policy_context)
            normalized.setdefault("approved", True)
            normalized.setdefault("policy_ids", [])
            context["policy_context"] = normalized
        return context

    def create_job(self, task: str, *, schedule: str = "manual", runtime_budget_seconds: int = 60, created_at: Optional[datetime] = None, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        created_at_value = (created_at or datetime.now(timezone.utc)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        job_id = f"job-{uuid.uuid4().hex[:8]}"
        normalized_payload = self._normalize_policy_context(payload)
        return self.job_store.create(
            job_id,
            task=task,
            schedule=schedule,
            runtime_budget_seconds=runtime_budget_seconds,
            status="pending",
            created_at=created_at_value,
            payload=normalized_payload,
        )

    def start_job(self, job_id: str, executor: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
        job = self.job_store.get(job_id)
        if job is None:
            raise KeyError(f"Unknown job: {job_id}")

        with self._active_jobs_lock:
            if job_id in self._active_jobs:
                return {
                    **job,
                    "status": "job_already_running",
                    "execution_status": {
                        "status": "job_already_running",
                        "stop_reason": "concurrent_execution_blocked",
                    },
                }
            self._active_jobs.add(job_id)

        lock_owner_id = f"{job_id}:{uuid.uuid4().hex[:12]}"
        process_lock = self._acquire_process_job_lock(job_id, lock_owner_id, lease_seconds=int(job.get("runtime_budget_seconds") or 60))
        if process_lock is None:
            # If lock acquisition failed, attempt a safe reclaim of the lock for this job
            try:
                reclaimed = None
                if hasattr(self.lock_backend, "safe_claim"):
                    reclaimed = self.lock_backend.safe_claim(job_id, lock_owner_id, lease_seconds=int(job.get("runtime_budget_seconds") or 60), reclaim_grace_seconds=1)
                if reclaimed:
                    # record recovery event
                    try:
                        payload = dict(job.get("payload", {}))
                        lock_events = list(payload.get("lock_events") or [])
                        lock_events.append({
                            "event": "lock_recovered",
                            "job_id": job_id,
                            "recovered_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                            "previous_owner": reclaimed.get("owner_id"),
                        })
                        payload["lock_events"] = lock_events
                        self.job_store.update(job_id, payload=payload)
                    except Exception:
                        pass
                    # we reclaimed — try acquiring file+db lock again
                    process_lock = self._acquire_process_job_lock(job_id, lock_owner_id, lease_seconds=int(job.get("runtime_budget_seconds") or 60))
                else:
                    # record contention event for observability
                    try:
                        payload = dict(job.get("payload", {}))
                        lock_events = list(payload.get("lock_events") or [])
                        lock_events.append({
                            "event": "lock_contention",
                            "job_id": job_id,
                            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                        })
                        payload["lock_events"] = lock_events
                        self.job_store.update(job_id, payload=payload)
                    except Exception:
                        pass
            except Exception:
                process_lock = None

            if process_lock is None:
                with self._active_jobs_lock:
                    self._active_jobs.discard(job_id)
                return {
                    **job,
                    "status": "job_already_running",
                    "execution_status": {
                        "status": "job_already_running",
                        "stop_reason": "concurrent_execution_blocked",
                    },
                }

        # If we successfully acquired a lease that reclaimed an expired lock, record an event
        try:
            lease_info = process_lock.get("lease") if isinstance(process_lock, dict) else None
            if isinstance(lease_info, dict) and lease_info.get("reclaimed"):
                payload = dict(job.get("payload", {}))
                lock_events = list(payload.get("lock_events") or [])
                lock_events.append({
                    "event": "lock_recovered",
                    "job_id": job_id,
                    "reclaimed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "previous_owner": lease_info.get("owner_id"),
                })
                payload["lock_events"] = lock_events
                self.job_store.update(job_id, payload=payload)
        except Exception:
            pass

        executor_pool = ThreadPoolExecutor(max_workers=1)
        lock_payload = None
        try:
            # record lock acquisition event for observability
            try:
                if lock_payload and isinstance(lock_payload, dict):
                    payload = dict(job.get("payload", {}))
                    lock_events = list(payload.get("lock_events") or [])
                    lock_events.append({
                        "event": "lock_acquired",
                        "job_id": job_id,
                        "owner_id": lock_payload.get("owner_id"),
                        "acquired_at": lock_payload.get("acquired_at"),
                    })
                    payload["lock_events"] = lock_events
                    self.job_store.update(job_id, payload=payload)
            except Exception:
                pass

            future = executor_pool.submit(executor, job.get("payload", {}))
            try:
                result = future.result(timeout=max(0, int(job.get("runtime_budget_seconds") or 0)))
            except TimeoutError:
                future.cancel()
                result = {
                    "status": "budget_exceeded",
                    "summary": "Job runtime budget was exceeded.",
                    "stop_reason": "runtime_budget_exceeded",
                }
            lock_payload = process_lock.get("lease") if isinstance(process_lock, dict) else None
        finally:
            executor_pool.shutdown(wait=False, cancel_futures=True)
            self.lock_backend.release(job_id, lock_owner_id)
            self._release_process_job_lock(process_lock)
            with self._active_jobs_lock:
                self._active_jobs.discard(job_id)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        # refresh payload from store in case we updated it (e.g. lock recovery events)
        latest_job = self.job_store.get(job_id)
        refreshed_payload = dict(latest_job.get("payload", {}) if latest_job is not None else job.get("payload", {}))
        updated_payload = {**refreshed_payload, **result}
        # ensure lock events are carried forward and include final lock acquisition if available
        try:
            lock_events = list(updated_payload.get("lock_events") or [])
            if lock_payload and isinstance(lock_payload, dict):
                lock_events.append({
                    "event": "lock_acquired",
                    "job_id": job_id,
                    "owner_id": lock_payload.get("owner_id"),
                    "acquired_at": lock_payload.get("acquired_at"),
                })
            if lock_events:
                updated_payload["lock_events"] = lock_events
        except Exception:
            pass
        execution_status = {
            "status": result.get("status", "completed"),
            "task_status": result.get("task_status"),
            "status_schema_version": result.get("status_schema_version"),
            "checkpoint_id": result.get("checkpoint"),
            "resume_hint": result.get("resume_hint") or {"can_resume": bool(result.get("checkpoint")), "resume_target": result.get("checkpoint")},
            "summary": result.get("summary"),
            "stop_reason": result.get("stop_reason"),
            "last_run_at": now,
            "lock_owner_id": lock_payload.get("owner_id") if isinstance(lock_payload, dict) else None,
            "lock": {
                "owner_id": lock_payload.get("owner_id") if isinstance(lock_payload, dict) else None,
                "acquired_at": lock_payload.get("acquired_at") if isinstance(lock_payload, dict) else None,
                "expires_at": lock_payload.get("expires_at") if isinstance(lock_payload, dict) else None,
                "lease_seconds": lock_payload.get("lease_seconds") if isinstance(lock_payload, dict) else None,
            },
        }
        updated_payload["execution_status"] = execution_status
        stored_job = self.job_store.update(
            job_id,
            status=result.get("status", "completed"),
            last_run_at=now,
            payload=updated_payload,
        )
        stored_job["summary"] = updated_payload.get("summary")
        stored_job["checkpoint"] = updated_payload.get("checkpoint")
        stored_job["task_status"] = updated_payload.get("task_status")
        stored_job["status_schema_version"] = updated_payload.get("status_schema_version")
        stored_job["execution_status"] = execution_status
        return stored_job

    def resume_job(self, job_id: str, executor: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
        job = self.job_store.get(job_id)
        if job is None:
            raise KeyError(f"Unknown job: {job_id}")
        payload = dict(job.get("payload", {}))
        execution_status = payload.get("execution_status") or {}
        resume_hint = execution_status.get("resume_hint") or {"can_resume": False}
        if not resume_hint.get("can_resume"):
            raise ValueError(f"Job '{job_id}' has no resumable checkpoint")
        resumed_payload = dict(payload)
        resumed_payload["resume_from"] = execution_status.get("checkpoint_id") or payload.get("checkpoint")
        resumed_payload["resumed_from_job_id"] = job_id
        return self.start_job(job_id, lambda previous_payload: executor({**previous_payload, **resumed_payload}))

    def run_due_jobs(self, executor: Callable[[Dict[str, Any]], Dict[str, Any]], now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        current_time = now or datetime.now(timezone.utc)
        due_jobs = self.job_store.list_due(current_time)
        completed_jobs: List[Dict[str, Any]] = []
        for job in due_jobs:
            completed_jobs.append(self.start_job(job["job_id"], executor))
        return completed_jobs


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
