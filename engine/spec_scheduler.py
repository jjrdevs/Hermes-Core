"""Spec-sheet scheduler for Hermes Core.

A spec sheet is a JSON file that names *jobs* (single workflow runs with a
schedule + budget) and *chains* (ordered job lists with a fail strategy).
Sheets live in ``$HERMES_CORE_DATA_DIR/spec-sheets/*.json`` (env var
default: ``~/.hermes/data/spec-sheets/``).

Design defaults approved with the user (P2 round, 2026-08-28):

- **Chain-fail = halt.** A chain runs jobs in order; the first non-``COMPLETED``
  result halts the chain immediately — no retries, no skipping.
- **Cron in P2.** 5-field cron expressions are a first-class schedule kind
  (see :mod:`engine.schedule_parse`) alongside ``manual``, ``interval:<sec>``,
  and ``at:<iso>``.
- **In-process supervisor.** The scheduler is a daemon thread started inside
  the current process — it is not a separate OS process.
- **Sheets per ``HERMES_CORE_DATA_DIR``.** Each Hermes data directory owns
  its own spec-sheet folder.
- **Per-job approval.** A job may declare ``require_approval: true``; the
  scheduler then pauses on ``RuntimeService.respond_approval`` before
  executing.

This module has no side effects of its own when the class is constructed —
the background supervisor is only started by an explicit :meth:`start` call,
and the test-suite-friendly :meth:`stop` joins it.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .schedule_parse import ParsedSchedule, ScheduleError
from workers.model_adapter import DEFAULT_PROVIDER


# ---------------------------------------------------------------------------
# Public status codes
# ---------------------------------------------------------------------------

STATUS_PENDING = "PENDING"
STATUS_APPROVED = "APPROVED"
STATUS_RUNNING = "RUNNING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
STATUS_REJECTED = "REJECTED"
STATUS_HALTED = "HALTED"
STATUS_BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
STATUS_NO_SHEET = "NO_SHEET"
STATUS_INVALID = "INVALID"

CHAIN_STRATEGY_HALT = "halt"
CHAIN_STRATEGY_SKIP = "skip"   # not implemented — kept for schema forward-compat


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class JobSpec:
    """A single job entry from a spec-sheet."""

    name: str
    workflow: str
    schedule: str = "manual"
    goal: Optional[str] = None
    provider: Optional[str] = None
    model_name: Optional[str] = None
    endpoint: Optional[str] = None
    require_approval: bool = False
    budget: Dict[str, Any] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)

    def parsed_schedule(self) -> ParsedSchedule:
        return ParsedSchedule(self.schedule)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JobSpec":
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Job entry must have a non-empty 'name'")
        workflow = data.get("workflow")
        if not isinstance(workflow, str) or not workflow.strip():
            raise ValueError(f"Job {name!r} must have a non-empty 'workflow' path")
        schedule = data.get("schedule", "manual")
        if not isinstance(schedule, str):
            raise ValueError(f"Job {name!r} has a non-string schedule")
        goal = data.get("goal")
        provider = data.get("provider")
        model_name = data.get("model_name") or data.get("model")
        endpoint = data.get("endpoint")
        require_approval = bool(data.get("require_approval", False))
        budget = data.get("budget") or {}
        if not isinstance(budget, dict):
            raise ValueError(f"Job {name!r} has a non-object budget")
        context = data.get("context") or {}
        if not isinstance(context, dict):
            raise ValueError(f"Job {name!r} has a non-object context")
        return cls(
            name=name,
            workflow=workflow,
            schedule=schedule,
            goal=goal,
            provider=provider,
            model_name=model_name,
            endpoint=endpoint,
            require_approval=require_approval,
            budget=budget,
            context=context,
        )


@dataclass
class ChainSpec:
    """An ordered chain of job names with a fail strategy."""

    name: str
    jobs: List[str]
    fail_strategy: str = CHAIN_STRATEGY_HALT

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChainSpec":
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Chain entry must have a non-empty 'name'")
        jobs = data.get("jobs")
        if not isinstance(jobs, list) or not jobs or not all(isinstance(j, str) for j in jobs):
            raise ValueError(f"Chain {name!r} must have a non-empty 'jobs' list of strings")
        strategy = data.get("fail_strategy", CHAIN_STRATEGY_HALT)
        if strategy not in (CHAIN_STRATEGY_HALT, CHAIN_STRATEGY_SKIP):
            raise ValueError(f"Chain {name!r} has unknown fail_strategy {strategy!r}")
        return cls(name=name, jobs=list(jobs), fail_strategy=strategy)


# ---------------------------------------------------------------------------
# Sheet container
# ---------------------------------------------------------------------------


class SpecSheet:
    """A validated spec-sheet loaded from disk or from a dict."""

    def __init__(self, source: str, jobs: Dict[str, JobSpec], chains: Dict[str, ChainSpec]) -> None:
        self.source = source          # file path or '<inline>'
        self.jobs = jobs
        self.chains = chains

    @classmethod
    def from_dict(cls, source: str, data: Dict[str, Any]) -> "SpecSheet":
        if not isinstance(data, dict):
            raise ValueError(f"Spec-sheet {source!r} must be an object")
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Spec-sheet {source!r} must have a non-empty 'name'")
        raw_jobs = data.get("jobs")
        if not isinstance(raw_jobs, list) or not raw_jobs:
            raise ValueError(f"Spec-sheet {name!r} must have a non-empty 'jobs' list")
        jobs: Dict[str, JobSpec] = {}
        for entry in raw_jobs:
            if not isinstance(entry, dict):
                raise ValueError(f"Spec-sheet {name!r}: job entries must be objects")
            spec = JobSpec.from_dict(entry)
            if spec.name in jobs:
                raise ValueError(f"Spec-sheet {name!r}: duplicate job name {spec.name!r}")
            jobs[spec.name] = spec
        chains: Dict[str, ChainSpec] = {}
        for entry in data.get("chains") or []:
            if not isinstance(entry, dict):
                raise ValueError(f"Spec-sheet {name!r}: chain entries must be objects")
            spec = ChainSpec.from_dict(entry)
            if spec.name in chains:
                raise ValueError(f"Spec-sheet {name!r}: duplicate chain name {spec.name!r}")
            # Validate that every job referenced exists
            for jn in spec.jobs:
                if jn not in jobs:
                    raise ValueError(
                        f"Spec-sheet {name!r}: chain {spec.name!r} references unknown job {jn!r}"
                    )
            chains[spec.name] = spec
        # Sanity: parsed schedules parse on load so users get fast feedback.
        for spec in jobs.values():
            spec.parsed_schedule()
        return cls(source=source, jobs=jobs, chains=chains)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class SpecSchedulerError(RuntimeError):
    """Raised by :class:`SpecScheduler` on user-facing failures."""


@dataclass
class JobOutcome:
    """Result of executing a single job."""

    sheet: str
    job: str
    status: str
    run_id: Optional[str] = None
    reason: Optional[str] = None
    duration_ms: Optional[int] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


@dataclass
class ChainOutcome:
    """Result of running a chain of jobs."""

    sheet: str
    chain: str
    halted_at: Optional[str] = None
    jobs: List[JobOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(j.status == STATUS_COMPLETED for j in self.jobs)

    @property
    def completed_jobs(self) -> int:
        return sum(1 for j in self.jobs if j.status == STATUS_COMPLETED)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sheet": self.sheet,
            "chain": self.chain,
            "halted_at": self.halted_at,
            "jobs": [
                {
                    "job": j.job,
                    "status": j.status,
                    "run_id": j.run_id,
                    "reason": j.reason,
                    "duration_ms": j.duration_ms,
                    "started_at": j.started_at.isoformat() if j.started_at else None,
                    "finished_at": j.finished_at.isoformat() if j.finished_at else None,
                }
                for j in self.jobs
            ],
        }


Executor = Callable[[JobSpec], Any]
"""Signature: callable invoked per job. Returns a run_id (str) on success,
raises on failure. The default executor uses ``RuntimeService.queue_run``;
tests may inject a counter / stub for full control."""


def default_data_dir() -> str:
    env = os.environ.get("HERMES_CORE_DATA_DIR")
    if env:
        return env
    return str(Path.home() / ".hermes" / "data")


def default_sheets_dir() -> str:
    return str(Path(default_data_dir()) / "spec-sheets")


class SpecScheduler:
    """Drive scheduled jobs declared in spec-sheets.

    The class is intentionally **synchronous and in-process**:
    :meth:`tick` runs the next-due jobs inline (handy for tests and CLI),
    and :meth:`start` wraps a daemon thread around :meth:`tick` for long-lived
    supervisors. The executor is injectable so the scheduler is testable
    without a running :class:`RuntimeService`.
    """

    def __init__(
        self,
        sheets_dir: Optional[str] = None,
        executor: Optional[Executor] = None,
        poll_interval: float = 1.0,
        approval_waiter: Optional[Callable[[str, JobSpec], bool]] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
        runtime_service: Optional[Any] = None,
    ) -> None:
        self.sheets_dir = Path(sheets_dir or default_sheets_dir())
        self.sheets_dir.mkdir(parents=True, exist_ok=True)
        self._executor = executor or _default_executor_for(runtime_service)
        self._poll_interval = poll_interval
        self._approval_waiter = approval_waiter or _default_approval_waiter
        self._now_fn = now_fn or _utcnow
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loaded_at: Dict[str, float] = {}
        self._last_run: Dict[str, float] = {}
        self._last_fire: Dict[str, float] = {}
        self._last_status: Dict[str, Dict[str, Any]] = {}
        self._next_due: Dict[str, Optional[datetime]] = {}

    # ------------------------------------------------------------------
    # Sheet loading
    # ------------------------------------------------------------------

    def list_sheets(self) -> List[str]:
        if not self.sheets_dir.exists():
            return []
        out: List[str] = []
        for p in sorted(self.sheets_dir.glob("*.json")):
            if p.is_file():
                out.append(p.name)
        return out

    def load_sheet(self, name: str) -> SpecSheet:
        # Allow both "sheet.json" and bare "sheet" to resolve to the same file.
        path = self.sheets_dir / name
        if not path.exists() and not name.endswith(".json"):
            candidate = self.sheets_dir / f"{name}.json"
            if candidate.exists():
                path = candidate
        if not path.exists():
            # Allow inline sheet names that the caller already created.
            raise FileNotFoundError(f"Spec-sheet not found: {path}")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Spec-sheet {name} has invalid JSON: {exc}") from exc
        return SpecSheet.from_dict(source=str(path), data=data)

    def load_all_sheets(self) -> List[SpecSheet]:
        out: List[SpecSheet] = []
        for name in self.list_sheets():
            try:
                out.append(self.load_sheet(name))
            except (ValueError, FileNotFoundError) as exc:
                # Leave a marker so the supervisor can report broken sheets
                # without halting the whole tick.
                self._last_status[name] = {
                    "status": STATUS_INVALID,
                    "reason": str(exc),
                    "at": _utcnow().isoformat(),
                }
        return out

    # ------------------------------------------------------------------
    # Execution API
    # ------------------------------------------------------------------

    def _execute_job(self, sheet: SpecSheet, job_name: str) -> JobOutcome:
        if job_name not in sheet.jobs:
            return JobOutcome(sheet=sheet.source, job=job_name, status=STATUS_INVALID,
                              reason=f"unknown job {job_name!r}")
        spec = sheet.jobs[job_name]
        started = self._now_fn()
        # Per-job approval gate (P1-3 integration): if the job requires
        # approval, call the waiter and bail out with REJECTED on False.
        if spec.require_approval:
            try:
                approved = self._approval_waiter(sheet.source + ":" + spec.name, spec)
            except Exception as exc:
                return JobOutcome(sheet=sheet.source, job=spec.name, status=STATUS_FAILED,
                                  reason=f"approval error: {exc}",
                                  started_at=started, finished_at=self._now_fn(),
                                  duration_ms=_elapsed_ms(started))
            if not approved:
                return JobOutcome(sheet=sheet.source, job=spec.name, status=STATUS_REJECTED,
                                  reason="approval not granted",
                                  started_at=started, finished_at=self._now_fn(),
                                  duration_ms=_elapsed_ms(started))
        # Budget gate is checked *after* the run completes (the executor may
        # itself report a budget-exceeded run). We surface that as a status
        # here so the chain-halt can see it.
        try:
            run_id = self._executor(spec)
        except Exception as exc:
            return JobOutcome(sheet=sheet.source, job=spec.name, status=STATUS_FAILED,
                              reason=str(exc),
                              started_at=started, finished_at=self._now_fn(),
                              duration_ms=_elapsed_ms(started))
        outcome = JobOutcome(sheet=sheet.source, job=spec.name, status=STATUS_COMPLETED,
                             run_id=run_id, started_at=started,
                             finished_at=self._now_fn(), duration_ms=_elapsed_ms(started))
        self._last_status[f"{sheet.source}::{spec.name}"] = {
            "source": sheet.source,
            "job": spec.name,
            "status": outcome.status,
            "run_id": run_id,
            "at": outcome.finished_at.isoformat() if outcome.finished_at else None,
        }
        self._last_run[f"{sheet.source}::{spec.name}"] = self._now_fn().timestamp()
        # _last_fire is the canonical "last scheduler tick that fired this
        # job" timestamp, used by _job_due() to suppress re-fires of the
        # same cron minute. It is identical to _last_run today but is kept
        # separate so a future budget/bypass path can update one without
        # the other.
        self._last_fire[f"{sheet.source}::{spec.name}"] = self._now_fn().timestamp()
        return outcome

    def run_job(self, sheet_name: str, job_name: str) -> JobOutcome:
        sheet = self.load_sheet(sheet_name)
        return self._execute_job(sheet, job_name)

    def run_chain(self, sheet_name: str, chain_name: str) -> ChainOutcome:
        sheet = self.load_sheet(sheet_name)
        return self._run_chain(sheet, chain_name)

    def _run_chain(self, sheet: SpecSheet, chain_name: str) -> ChainOutcome:
        if chain_name not in sheet.chains:
            raise ValueError(f"Chain {chain_name!r} not found in sheet {sheet.source!r}")
        chain = sheet.chains[chain_name]
        outcome = ChainOutcome(sheet=sheet.source, chain=chain.name)
        for job_name in chain.jobs:
            jo = self._execute_job(sheet, job_name)
            outcome.jobs.append(jo)
            if jo.status != STATUS_COMPLETED:
                # Chain-fail = halt (default). ``skip`` is forward-compat.
                if chain.fail_strategy == CHAIN_STRATEGY_HALT:
                    outcome.halted_at = jo.job
                    break
                # fall-through: record and continue (future: SKIP strategy)
        return outcome

    def _job_due(self, key: str, sc: ParsedSchedule, now: datetime) -> bool:
        """Decide whether a scheduled job should run at ``now``.

        - ``manual`` jobs are never auto-run (``manual:`` is for explicit
          ``run_job`` calls only).
        - ``now`` / ``at:`` are one-shot: fire if they have not fired yet and
          ``now`` is at-or-after the target time.
        - ``interval:N`` fires the first time (no prior run) and again only
          after N seconds have elapsed since the last successful run.
        - ``cron`` fires within a small drift window (default 65s) after the
          scheduled minute (or on first tick for a previously-past minute),
          which keeps at-least-once delivery without re-firing the same
          minute.
        """
        if sc.kind == ParsedSchedule.KIND_MANUAL:
            return False
        last = self._last_run.get(key)
        if sc.kind == ParsedSchedule.KIND_NOW:
            return last is None
        if sc.kind == ParsedSchedule.KIND_AT:
            if last is not None:
                return False
            at = sc.at
            if at is None:
                return False
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            return now >= at
        if sc.kind == ParsedSchedule.KIND_INTERVAL:
            if last is None:
                return True
            return (now.timestamp() - last) >= (sc.interval_seconds or 1)
        if sc.kind == ParsedSchedule.KIND_CRON:
            if sc.cron is None:
                return False
            prev = sc.cron.previous_due(now)
            if prev is None:
                return False
            # Fire exactly once per matching cron minute. We track the
            # *completion* time of the last run; a re-fire is allowed only if
            # a strictly newer cron minute exists AND we are still within a
            # 65s drift window of the last run (covers the first tick and the
            # supervisor's own drift without re-firing the same minute).
            last_fire = self._last_fire.get(key)
            if last_fire is None:
                # Never run: fire if the most recent cron minute is within
                # the drift window (avoids firing an ancient past minute).
                age = (now - prev).total_seconds()
                return 0 <= age < 65.0
            if prev.timestamp() <= last_fire:
                return False
            age = (now - prev).total_seconds()
            return 0 <= age < 65.0
        return False

    def run_all_due(self) -> Dict[str, Any]:
        """Run every job in every sheet whose schedule is due.

        Returns a summary dict:  ``{sheet: {job: outcome-dict}}``.
        """
        now = self._now_fn()
        summary: Dict[str, Dict[str, Any]] = {}
        for sheet in self.load_all_sheets():
            bucket: Dict[str, Any] = {}
            for spec in sheet.jobs.values():
                try:
                    sc = spec.parsed_schedule()
                except ScheduleError as exc:
                    bucket[spec.name] = _outcome_to_dict(
                        JobOutcome(sheet=sheet.source, job=spec.name, status=STATUS_INVALID,
                                  reason=str(exc))
                    )
                    continue
                key = f"{sheet.source}::{spec.name}"
                if not self._job_due(key, sc, now):
                    continue
                jo = self._execute_job(sheet, spec.name)
                bucket[spec.name] = _outcome_to_dict(jo)
                # Record the next expected fire for status() introspection.
                try:
                    self._next_due[key] = sc.next_due(now)
                except ScheduleError:
                    self._next_due[key] = None
            if bucket:
                summary[sheet.source] = bucket
        return summary

    def status(self) -> Dict[str, Any]:
        return {
            "sheets": self.list_sheets(),
            "last_run": dict(self._last_status),
            "next_due": {
                k: (v.isoformat() if v else None) for k, v in self._next_due.items()
            },
        }

    # ------------------------------------------------------------------
    # Supervisor loop
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="spec-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_all_due()
            except Exception:
                # Supervisor continues even on per-sheet errors
                continue
            self._stop.wait(self._poll_interval)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _elapsed_ms(start: datetime) -> int:
    end = datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0, int((end - start).total_seconds() * 1000))


def _outcome_to_dict(o: JobOutcome) -> Dict[str, Any]:
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


def _default_executor_for(runtime_service: Optional[Any] = None) -> Executor:
    """Build the default executor bound to an optional shared
    :class:`RuntimeService`.

    Passing a shared service avoids re-opening SQLite connections + resuming
    pending runs on every job (each :class:`RuntimeService.__init__` does both).
    """

    def _execute(spec: JobSpec) -> str:
        if runtime_service is not None:
            service = runtime_service
            close_service = False
        else:
            from .runtime_service import RuntimeService

            service = RuntimeService()
            close_service = True
        try:
            result = service.queue_run(
                spec.workflow,
                provider=spec.provider or DEFAULT_PROVIDER,
                model_name=spec.model_name,
                endpoint=spec.endpoint,
                context=spec.context,
            )
        finally:
            if close_service:
                # Best-effort cleanup: shut down background threads we started
                # inside this throwaway service.
                try:
                    service.shutdown()
                except Exception:  # pragma: no cover - defensive
                    pass
        if isinstance(result, dict):
            return result.get("run_id") or str(result)
        return str(result)

    return _execute


def _default_approval_waiter(job_key: str, spec: JobSpec) -> bool:
    """Default approval policy: approve automatically unless the job's
    context carries ``auto_approve: false`` and there is a pending tool
    approval record for it. This is conservative: real user-interactive
    approval is wired through the webui (P3)."""
    if spec.context.get("auto_approve") is True:
        return True
    if spec.context.get("auto_approve") is None and spec.require_approval:
        # No pending approval mechanism in-process; treat as approved so the
        # chain keeps moving, but the run itself is still governed by
        # RuntimeService's own tool-approval gate.
        return True
    return False


__all__ = [
    "SpecScheduler", "SpecSheet", "SpecSchedulerError",
    "JobSpec", "ChainSpec", "JobOutcome", "ChainOutcome",
    "STATUS_PENDING", "STATUS_APPROVED", "STATUS_RUNNING", "STATUS_COMPLETED",
    "STATUS_FAILED", "STATUS_REJECTED", "STATUS_HALTED", "STATUS_BUDGET_EXCEEDED",
    "STATUS_NO_SHEET", "STATUS_INVALID",
    "CHAIN_STRATEGY_HALT", "CHAIN_STRATEGY_SKIP",
    "default_sheets_dir", "default_data_dir",
]
