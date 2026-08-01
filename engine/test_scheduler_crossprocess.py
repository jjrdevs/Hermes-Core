import tempfile
import threading
import time
import unittest
from pathlib import Path

from engine.scheduler import SQLiteJobStore, SQLiteLeaseLockBackend, BackgroundScheduler


class TestCrossProcessLocking(unittest.TestCase):
    def test_two_schedulers_race_for_same_job(self):
        with tempfile.TemporaryDirectory(prefix="hermes-cross-", dir="/tmp") as td:
            job_db = Path(td) / "jobs.db"
            lock_db = Path(td) / "locks.db"
            store = SQLiteJobStore(job_db)
            lock_backend = SQLiteLeaseLockBackend(lock_db)

            sched1 = BackgroundScheduler(store, lock_backend=lock_backend)
            sched2 = BackgroundScheduler(store, lock_backend=lock_backend)

            job = sched1.create_job("task", schedule="manual", runtime_budget_seconds=2)
            job_id = job["job_id"]

            results = {}

            def run_scheduler(sched, name):
                def executor(payload):
                    time.sleep(0.1)
                    return {"status": "completed", "summary": f"ran by {name}"}

                results[name] = sched.start_job(job_id, executor)

            t1 = threading.Thread(target=run_scheduler, args=(sched1, "s1"))
            t2 = threading.Thread(target=run_scheduler, args=(sched2, "s2"))

            t1.start()
            t2.start()
            t1.join()
            t2.join()

            # One should have run, the other should have observed contention or job_already_running
            statuses = {results["s1"]["status"], results["s2"]["status"]}
            self.assertIn("completed", statuses)
            # ensure lock events persisted for contention or recovery
            stored = store.get(job_id)
            payload = stored.get("payload", {})
            self.assertIn("lock_events", payload)
            events = payload.get("lock_events")
            self.assertTrue(any(e.get("event") in ("lock_recovered", "lock_contention") for e in events))


if __name__ == "__main__":
    unittest.main()
