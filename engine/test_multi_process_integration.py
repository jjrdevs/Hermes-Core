import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from engine.scheduler import SQLiteJobStore, SQLiteLeaseLockBackend


class TestMultiProcessIntegration(unittest.TestCase):
    def test_two_processes_race(self):
        with tempfile.TemporaryDirectory(prefix="hermes-mp-", dir="/tmp") as td:
            job_db = Path(td) / "jobs.db"
            lock_db = Path(td) / "locks.db"
            store = SQLiteJobStore(job_db)
            lock_backend = SQLiteLeaseLockBackend(lock_db)

            job = store.create(job_id=f"job-{__import__('uuid').uuid4().hex[:8]}", task="t", schedule="manual", runtime_budget_seconds=2, status="pending", created_at=(__import__('datetime').datetime.now().__format__('%Y-%m-%dT%H:%M:%SZ')), payload={})
            job_id = job["job_id"]

            # launch two processes
            p1 = subprocess.Popen([sys.executable, "engine/worker_process.py", str(job_db), str(lock_db), job_id, "p1"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            p2 = subprocess.Popen([sys.executable, "engine/worker_process.py", str(job_db), str(lock_db), job_id, "p2"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            out1, err1 = p1.communicate(timeout=10)
            out2, err2 = p2.communicate(timeout=10)

            # one should have completed and job payload should have lock_events
            stored = store.get(job_id)
            payload = stored.get("payload", {})
            self.assertIn("lock_events", payload)
            events = payload.get("lock_events")
            self.assertTrue(any(e.get("event") in ("lock_recovered", "lock_contention", "lock_acquired") for e in events))


if __name__ == '__main__':
    unittest.main()
