import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine.scheduler import BackgroundScheduler, LocalLeaseLockBackend
from engine.storage import SQLiteJobStore


class TestBackgroundScheduler(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="hermes-jobs-", dir="/tmp")
        self.job_store = SQLiteJobStore(Path(self.temp_dir.name) / "jobs.db")
        self.scheduler = BackgroundScheduler(self.job_store)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_create_and_run_job(self):
        job = self.scheduler.create_job("Inspect the repository", schedule="manual", runtime_budget_seconds=30)

        self.assertEqual(job["status"], "pending")
        self.assertEqual(job["schedule"], "manual")

        completed = self.scheduler.start_job(
            job["job_id"],
            executor=lambda payload: {"status": "completed", "summary": "done", "checkpoint": "checkpoint-1"},
        )

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["summary"], "done")
        self.assertEqual(completed["checkpoint"], "checkpoint-1")

    def test_create_job_normalizes_policy_context(self):
        job = self.scheduler.create_job("Scheduled task", payload={"task": "inspect"})
        self.assertEqual(job["payload"]["policy_context"], {"approved": True, "policy_ids": []})

        invalid_job = self.scheduler.create_job("Invalid scheduled task", payload={"policy_context": "invalid"})
        self.assertEqual(invalid_job["payload"]["policy_context"]["approved"], False)
        self.assertEqual(invalid_job["payload"]["policy_context"]["reason"], "policy_context.invalid")

    def test_run_due_jobs_executes_pending_work(self):
        created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        job = self.scheduler.create_job("Check for regressions", schedule="interval:60", runtime_budget_seconds=10, created_at=created_at)

        started = self.scheduler.run_due_jobs(
            executor=lambda payload: {"status": "completed", "summary": "checked", "checkpoint": "checkpoint-2"},
            now=datetime.now(timezone.utc),
        )

        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["job_id"], job["job_id"])
        self.assertEqual(started[0]["status"], "completed")

    def test_start_job_builds_an_execution_status_snapshot(self):
        job = self.scheduler.create_job("Inspect the repository", schedule="manual", runtime_budget_seconds=30)

        completed = self.scheduler.start_job(
            job["job_id"],
            executor=lambda payload: {
                "status": "completed",
                "summary": "done",
                "checkpoint": "checkpoint-1",
                "resume_hint": {"can_resume": True, "resume_target": "checkpoint-1"},
            },
        )

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["summary"], "done")
        self.assertEqual(completed["checkpoint"], "checkpoint-1")
        self.assertEqual(completed["execution_status"]["resume_hint"]["can_resume"], True)
        self.assertEqual(completed["execution_status"]["checkpoint_id"], "checkpoint-1")

    def test_start_job_exposes_lock_owner_and_expiration_metadata(self):
        job = self.scheduler.create_job("Exclusive task", schedule="manual", runtime_budget_seconds=30)

        completed = self.scheduler.start_job(
            job["job_id"],
            executor=lambda payload: {"status": "completed", "summary": "done", "checkpoint": "checkpoint-lock"},
        )

        self.assertIn("lock_owner_id", completed["execution_status"])
        self.assertIn("lock", completed["execution_status"])
        self.assertIn("expires_at", completed["execution_status"]["lock"])

    def test_start_job_preserves_canonical_task_status(self):
        job = self.scheduler.create_job("Verify repository task", schedule="manual", runtime_budget_seconds=30)

        completed = self.scheduler.start_job(
            job["job_id"],
            executor=lambda payload: {
                "status": "completed",
                "task_status": "COMPLETED_VERIFIED",
                "status_schema_version": 1,
                "summary": "verified",
            },
        )

        self.assertEqual(completed["execution_status"]["task_status"], "COMPLETED_VERIFIED")
        self.assertEqual(completed["execution_status"]["status_schema_version"], 1)

    def test_resume_job_reuses_the_latest_checkpoint_payload(self):
        job = self.scheduler.create_job("Inspect the repository", schedule="manual", runtime_budget_seconds=30)

        self.scheduler.start_job(
            job["job_id"],
            executor=lambda payload: {
                "status": "completed",
                "summary": "done",
                "checkpoint": "checkpoint-2",
                "resume_hint": {"can_resume": True, "resume_target": "checkpoint-2"},
            },
        )

        resumed = self.scheduler.resume_job(
            job["job_id"],
            executor=lambda payload: {
                "status": "completed",
                "summary": payload.get("resume_from"),
                "checkpoint": "checkpoint-3",
                "resume_hint": {"can_resume": True, "resume_target": "checkpoint-3"},
            },
        )

        self.assertEqual(resumed["summary"], "checkpoint-2")
        self.assertEqual(resumed["execution_status"]["checkpoint_id"], "checkpoint-3")

    def test_start_job_enforces_runtime_budget(self):
        job = self.scheduler.create_job("Bounded task", schedule="manual", runtime_budget_seconds=0)

        result = self.scheduler.start_job(
            job["job_id"],
            executor=lambda payload: (time.sleep(0.05) or {"status": "completed", "summary": "late"}),
        )

        self.assertEqual(result["status"], "budget_exceeded")
        self.assertEqual(result["execution_status"]["stop_reason"], "runtime_budget_exceeded")

    def test_start_job_blocks_concurrent_execution_of_same_job(self):
        job = self.scheduler.create_job("Exclusive task", schedule="manual", runtime_budget_seconds=2)
        started = threading.Event()
        release = threading.Event()
        results = []

        def executor(payload):
            started.set()
            release.wait(timeout=1)
            return {"status": "completed", "summary": "done"}

        worker = threading.Thread(target=lambda: results.append(self.scheduler.start_job(job["job_id"], executor)))
        worker.start()
        self.assertTrue(started.wait(timeout=1))

        concurrent = self.scheduler.start_job(job["job_id"], executor)
        self.assertEqual(concurrent["status"], "job_already_running")
        self.assertEqual(concurrent["execution_status"]["stop_reason"], "concurrent_execution_blocked")

        release.set()
        worker.join(timeout=2)
        self.assertEqual(results[0]["status"], "completed")

    def test_start_job_blocks_concurrent_execution_across_scheduler_instances(self):
        job = self.scheduler.create_job("Cross-process task", schedule="manual", runtime_budget_seconds=2)
        second_store = SQLiteJobStore(Path(self.temp_dir.name) / "jobs.db")
        second_scheduler = BackgroundScheduler(second_store)
        started = threading.Event()
        release = threading.Event()
        results = []

        def executor(payload):
            started.set()
            release.wait(timeout=1)
            return {"status": "completed", "summary": "done"}

        worker = threading.Thread(target=lambda: results.append(self.scheduler.start_job(job["job_id"], executor)))
        worker.start()
        self.assertTrue(started.wait(timeout=1))

        concurrent = second_scheduler.start_job(job["job_id"], executor)
        self.assertEqual(concurrent["status"], "job_already_running")
        self.assertEqual(concurrent["execution_status"]["stop_reason"], "concurrent_execution_blocked")

        release.set()
        worker.join(timeout=2)
        second_store.close()
        self.assertEqual(results[0]["status"], "completed")

    def test_local_lease_lock_backend_reclaims_expired_lock(self):
        backend = LocalLeaseLockBackend(Path(self.temp_dir.name))
        lock_path = Path(self.temp_dir.name) / ".hermes-job-job-expired.lock"
        lock_path.write_text(
            json.dumps(
                {
                    "job_id": "job-expired",
                    "owner_id": "stale-owner",
                    "acquired_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
                    "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                    "lease_seconds": 30,
                }
            ),
            encoding="utf-8",
        )

        lock = backend.acquire("job-expired", "fresh-owner", lease_seconds=30)
        self.assertIsNotNone(lock)
        self.assertEqual(lock["owner_id"], "fresh-owner")
        backend.release("job-expired", "fresh-owner")

    def test_local_lease_lock_backend_rejects_active_owner(self):
        backend = LocalLeaseLockBackend(Path(self.temp_dir.name))
        first = backend.acquire("job-live", "owner-a", lease_seconds=30)
        self.assertIsNotNone(first)

        second = backend.acquire("job-live", "owner-b", lease_seconds=30)
        self.assertIsNone(second)
        backend.release("job-live", "owner-a")


if __name__ == "__main__":
    unittest.main()
