#!/usr/bin/env python3
import sys
import json
from pathlib import Path
import os
# ensure repo root is on sys.path when run as a script
repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))
from engine.scheduler import SQLiteJobStore, SQLiteLeaseLockBackend, BackgroundScheduler

# args: job_db lock_db job_id name
if __name__ == '__main__':
    job_db = Path(sys.argv[1])
    lock_db = Path(sys.argv[2])
    job_id = sys.argv[3]
    name = sys.argv[4]

    store = SQLiteJobStore(job_db)
    lock_backend = SQLiteLeaseLockBackend(lock_db)
    sched = BackgroundScheduler(store, lock_backend=lock_backend)

    def executor(payload):
        # write a small file to indicate who ran
        return {"status": "completed", "summary": f"ran by {name}"}

    result = sched.start_job(job_id, executor)
    print(json.dumps(result))
 