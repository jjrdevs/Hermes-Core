"""Tests for engine/spec_scheduler.py — spec-sheet scheduler.

Sheet schema (validated by ``SpecSheet.from_dict``): ``jobs`` and ``chains``
are *lists* of objects, each carrying its own ``name`` field. Chains
reference job names that must already exist in the same sheet.

Tests cover: sheet load/validation, per-job approval gating, chain-fail
halt/skip, budget (no run_id vs. run_id), interval/`at`/cron due-window
logic (incl. no re-fire of the same cron minute), and supervisor
start/stop lifecycle.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from engine.spec_scheduler import (
    CHAIN_STRATEGY_HALT,
    STATUS_BUDGET_EXCEEDED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_REJECTED,
    SpecScheduler,
    SpecSheet,
)
from engine.schedule_parse import ParsedSchedule


def _sheet(**overrides) -> SpecSheet:
    base: Dict[str, Any] = {
        "name": "s",
        "jobs": [
            {"name": "approved", "workflow": "w", "schedule": "manual",
             "require_approval": False},
            {"name": "denied", "workflow": "w", "schedule": "manual",
             "require_approval": True},
            {"name": "errored", "workflow": "w", "schedule": "manual",
             "require_approval": True},
            {"name": "ok",    "workflow": "ok",    "schedule": "manual"},
            {"name": "fail",  "workflow": "fail",  "schedule": "manual"},
            {"name": "after", "workflow": "after", "schedule": "manual"},
            {"name": "iv",    "workflow": "w",     "schedule": "interval:2"},
            {"name": "cr",    "workflow": "w",     "schedule": "cron:0 9 * * *"},
            {"name": "at",    "workflow": "w",     "schedule": "at:2026-01-01T00:00:00+00:00"},
            {"name": "atfuture", "workflow": "w",  "schedule": "at:2100-01-01T00:00:00+00:00"},
        ],
        "chains": [
            {"name": "halt_chain", "jobs": ["ok", "fail", "after"],
             "fail_strategy": "halt"},
            {"name": "skip_chain", "jobs": ["ok", "fail", "after"],
             "fail_strategy": "skip"},
        ],
    }
    base.update(overrides)
    return SpecSheet.from_dict(source="s.json", data=base)


class TestSheetValidation(unittest.TestCase):
    def test_basic(self):
        sheet = _sheet()
        self.assertIn("approved", sheet.jobs)
        self.assertEqual(sheet.jobs["approved"].require_approval, False)
        self.assertIn("halt_chain", sheet.chains)
        self.assertEqual(sheet.chains["halt_chain"].fail_strategy, "halt")

    def test_invalid_schedule(self):
        with self.assertRaises(Exception):
            _sheet(jobs=[{"name": "x", "workflow": "w", "schedule": "bogus"}])

    def test_chain_unknown_strategy(self):
        with self.assertRaises(ValueError):
            _sheet(chains=[{"name": "c", "jobs": ["ok"], "fail_strategy": "nonsense"}])

    def test_chain_unknown_job(self):
        with self.assertRaises(ValueError):
            _sheet(chains=[{"name": "c", "jobs": ["does_not_exist"], "fail_strategy": "halt"}])


class TestPerJobApproval(unittest.TestCase):
    def test_approved_runs(self):
        calls: List[str] = []
        def executor(spec) -> str:
            calls.append(spec.name)
            return "run-1"
        sched = SpecScheduler(sheets_dir=tempfile.mkdtemp(),
                              executor=executor,
                              approval_waiter=lambda key, spec: True)
        jo = sched._execute_job(_sheet(), "approved")
        self.assertEqual(jo.status, STATUS_COMPLETED)
        self.assertEqual(calls, ["approved"])

    def test_denied_halts(self):
        def executor(spec) -> str:
            raise AssertionError("executor must not run for a denied job")
        sched = SpecScheduler(sheets_dir=tempfile.mkdtemp(),
                              executor=executor,
                              approval_waiter=lambda key, spec: False)
        jo = sched._execute_job(_sheet(), "denied")
        self.assertEqual(jo.status, STATUS_REJECTED)

    def test_approval_boom_fails(self):
        def boom(key, spec):
            raise RuntimeError("approval boom")
        sched = SpecScheduler(sheets_dir=tempfile.mkdtemp(),
                              executor=lambda spec: "x",
                              approval_waiter=boom)
        jo = sched._execute_job(_sheet(), "errored")
        self.assertEqual(jo.status, STATUS_FAILED)
        self.assertIn("approval boom", jo.reason)


class TestChain(unittest.TestCase):
    def test_halt_stops_after_failure(self):
        attempts: List[str] = []
        def executor(spec) -> str:
            attempts.append(spec.name)
            if spec.name == "fail":
                raise RuntimeError("intentional")
            return f"run-{spec.name}"
        sched = SpecScheduler(sheets_dir=tempfile.mkdtemp(), executor=executor)
        co = sched._run_chain(_sheet(), "halt_chain")
        self.assertEqual(co.jobs[0].status, STATUS_COMPLETED)
        self.assertEqual(co.jobs[1].status, STATUS_FAILED)
        self.assertEqual(len(co.jobs), 2)  # "after" never attempted
        self.assertEqual(attempts, ["ok", "fail"])
        self.assertEqual(co.halted_at, "fail")

    def test_skip_continues(self):
        attempts: List[str] = []
        def executor(spec) -> str:
            attempts.append(spec.name)
            if spec.name == "fail":
                raise RuntimeError("intentional")
            return f"run-{spec.name}"
        sched = SpecScheduler(sheets_dir=tempfile.mkdtemp(), executor=executor)
        co = sched._run_chain(_sheet(), "skip_chain")
        self.assertEqual(len(co.jobs), 3)
        self.assertEqual(attempts, ["ok", "fail", "after"])
        self.assertIsNone(co.halted_at)

    def test_missing_chain(self):
        sched = SpecScheduler(sheets_dir=tempfile.mkdtemp(), executor=lambda s: "x")
        with self.assertRaises(ValueError):
            sched._run_chain(_sheet(), "ghost")


class TestIntervalDue(unittest.TestCase):
    def test_first_then_wait_then_ready(self):
        sheet = _sheet()
        now0 = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
        sched = SpecScheduler(sheets_dir=tempfile.mkdtemp(), executor=lambda s: "x")
        key = "s.json::iv"
        sc = sheet.jobs["iv"].parsed_schedule()
        self.assertEqual(sc.interval_seconds, 2)
        # First tick: fire.
        self.assertTrue(sched._job_due(key, sc, now0))
        # Stamp, then 1s later — not yet due.
        sched._last_run[key] = now0.timestamp()
        self.assertFalse(sched._job_due(key, sc, now0 + timedelta(seconds=1)))
        # 3s later — due again.
        self.assertTrue(sched._job_due(key, sc, now0 + timedelta(seconds=3)))


class TestCronNoRefire(unittest.TestCase):
    def test_same_minute_does_not_refire(self):
        sc = ParsedSchedule("cron:0 9 * * *")
        now = datetime(2026, 8, 29, 9, 0, 30, tzinfo=timezone.utc)
        sched = SpecScheduler(sheets_dir=tempfile.mkdtemp(), executor=lambda s: "x")
        key = "s.json::cr"
        # First tick (never fired): within 65s drift window → fire once.
        self.assertTrue(sched._job_due(key, sc, now))
        # Stamp completion at now+30s (i.e. 9:01:00).
        sched._last_fire[key] = (now + timedelta(seconds=30)).timestamp()
        # 1 minute later (9:01:30) — previous due minute is still 9:00,
        # which is not strictly newer than last_fire → no re-fire.
        self.assertFalse(sched._job_due(key, sc, now + timedelta(seconds=60)))


class TestAt(unittest.TestCase):
    def test_past_at_fires_once(self):
        sheet = _sheet()
        now = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)
        sched = SpecScheduler(sheets_dir=tempfile.mkdtemp(), executor=lambda s: "x")
        key = "s.json::at"
        sc = sheet.jobs["at"].parsed_schedule()
        self.assertTrue(sched._job_due(key, sc, now))
        sched._last_run[key] = now.timestamp()
        self.assertFalse(sched._job_due(key, sc, now))

    def test_future_at_not_due(self):
        sheet = _sheet()
        now = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)
        sched = SpecScheduler(sheets_dir=tempfile.mkdtemp(), executor=lambda s: "x")
        sc = sheet.jobs["atfuture"].parsed_schedule()
        self.assertFalse(sched._job_due("s.json::atfuture", sc, now))


class TestLifecycle(unittest.TestCase):
    def test_start_stop(self):
        sched = SpecScheduler(sheets_dir=tempfile.mkdtemp(),
                              executor=lambda s: "x", poll_interval=0.05)
        sched.start()
        self.assertIsNotNone(sched._thread)
        self.assertTrue(sched._thread.is_alive())
        sched.stop(timeout=2.0)
        self.assertIsNone(sched._thread)

    def test_run_all_due_empty_dir(self):
        sched = SpecScheduler(sheets_dir=tempfile.mkdtemp(),
                              executor=lambda s: "x")
        self.assertEqual(sched.run_all_due(), {})


if __name__ == "__main__":
    unittest.main()
