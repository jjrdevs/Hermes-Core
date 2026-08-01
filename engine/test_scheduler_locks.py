import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine.scheduler import BackgroundScheduler, SQLiteJobStore, SQLiteLeaseLockBackend


class TestSchedulerLocks(unittest.TestCase):
    def test_start_job_blocked_by_active_lock_and_recovers_after_expiry(self):
        with tempfile.TemporaryDirectory(prefix="hermes-sched-", dir="/tmp") as temp_dir:
            job_db = Path(temp_dir) / "jobs.db"
            lock_db = Path(temp_dir) / "locks.db"
            store = SQLiteJobStore(job_db)
            lock_backend = SQLiteLeaseLockBackend(lock_db)
            sched = BackgroundScheduler(store, lock_backend=lock_backend)

            # create job
            job = sched.create_job("echo hi", schedule="manual", runtime_budget_seconds=1)
            job_id = job["job_id"]

            # simulate another owner holding lock with future expiry
            other_owner = f"{job_id}:owner123"
            lock_backend.acquire(job_id, other_owner, lease_seconds=60)

            # starting job should detect lock and return job_already_running
            def executor(payload):
                return {"status": "completed", "summary": "ok"}

            result = sched.start_job(job_id, executor)
            self.assertEqual(result.get("status"), "job_already_running")

            # now expire the lock by force inserting an expired lock (simulate stale)
            # directly force release by inserting an expired entry then run recovery
            lock_backend.force_release(job_id)
            # create an expired lock entry to simulate stale (expires in past)
            expired_owner = f"{job_id}:owner-old"
            now = datetime.now(timezone.utc)
            acquired = (now - timedelta(seconds=120)).isoformat().replace("+00:00", "Z")
            expires = (now - timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
            # insert directly into sqlite
            conn = lock_backend.conn
            cur = conn.cursor()
            cur.execute("INSERT OR REPLACE INTO locks (job_id, owner_id, acquired_at, expires_at, lease_seconds) VALUES (?, ?, ?, ?, ?)", (job_id, expired_owner, acquired, expires, 60))
            conn.commit()

            # Starting job should trigger stale recovery and then run
            result2 = sched.start_job(job_id, executor)
            self.assertIn(result2.get("status"), {"completed", "budget_exceeded", "job_already_running"})
            # ensure lock_events recorded
            stored = store.get(job_id)
            payload = stored.get("payload", {})
            self.assertIn("lock_events", payload)
            events = payload.get("lock_events")
            self.assertTrue(any(e.get("event") == "lock_recovered" for e in events))


if __name__ == "__main__":
    unittest.main()
