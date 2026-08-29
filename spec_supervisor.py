#!/usr/bin/env python
"""Hermes spec-schedule supervisor.

Keeps a :class:`SpecScheduler` alive so spec-sheet jobs declared with an
``interval:<s>``, ``at:<iso>`` or ``cron:<field>`` schedule actually fire on
time.  The webui queue page (``/spec-schedule``) and the CLI (``spec-schedule
run``) are *on-demand* entry points; this service is what makes schedules
automatic.

Run under the systemd user unit ``hermes-spec-supervisor.service`` or directly:

    .venv/bin/python spec_supervisor.py [--data-dir DIR] [--sheets-dir DIR] [--poll 2.0]

Shared state (single service per machine):
    lockfile  ``<data-dir>/spec-supervisor.lock``  (flock; a second instance
              exits 75 - systemd's "service is already running" signal)

Signals:
    SIGINT / SIGTERM  -> stop the tick loop, then shut the shared
        RuntimeService down cleanly.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

log = logging.getLogger("spec-supervisor")


def _setup_logging(log_file: str | None) -> None:
    root = logging.getLogger()
    level = getattr(logging, os.environ.get("HERMES_SPEC_LOG_LEVEL", "INFO").upper(), logging.INFO)
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


class Supervisor:
    """Owns the shared RuntimeService + SpecScheduler and drives the tick loop."""

    def __init__(self, data_dir: str | None, sheets_dir: str | None, poll: float) -> None:
        from engine.runtime_service import RuntimeService  # local import: needs repo on path
        from engine.spec_scheduler import SpecScheduler, default_sheets_dir

        self.data_dir = data_dir
        # sheets-dir default must track --data-dir, not the global default.
        if sheets_dir:
            self.sheets_dir = Path(sheets_dir)
        elif data_dir:
            self.sheets_dir = Path(data_dir) / "spec-sheets"
        else:
            self.sheets_dir = Path(default_sheets_dir())
        self.sheets_dir.mkdir(parents=True, exist_ok=True)
        self._service = RuntimeService(data_dir=data_dir)
        self._scheduler = SpecScheduler(
            sheets_dir=str(self.sheets_dir),
            runtime_service=self._service,
            poll_interval=poll,
        )
        self._stop = threading.Event()

    # -- signaling -----------------------------------------------------
    def _on_signal(self, signum: int, _frame: object) -> None:
        name = signal.Signals(signum).name
        log.info("received %s - stopping", name)
        self._stop.set()

    # -- logging of one fire batch --------------------------------------
    def _report(self, fired: dict) -> None:
        for sheet_path, jobs in (fired or {}).items():
            for job_name, outcome in (jobs or {}).items():
                status = outcome.get("status", "?") if isinstance(outcome, dict) else "?"
                run_id = outcome.get("run_id") if isinstance(outcome, dict) else None
                detail = f" run_id={run_id}" if run_id else ""
                reason = outcome.get("reason") if isinstance(outcome, dict) else None
                if status in {"COMPLETED", "RUNNING", "QUEUED", "PENDING_APPROVAL"}:
                    log.info("fired %s -> %s%s", job_name, status, detail)
                else:
                    log.warning("fired %s -> %s%s reason=%s", job_name, status, detail, reason)

    # -- main loop -------------------------------------------------------
    def run(self) -> int:
        log.info(
            "starting: data_dir=%s sheets_dir=%s poll=%.1fs",
            self.data_dir or "(default)",
            self.sheets_dir,
            self._scheduler._poll_interval,
        )
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                fired = self._scheduler.run_all_due()
            except Exception:  # supervisor must survive any tick error
                log.exception("tick failed; continuing")
                fired = None
            if fired:
                self._report(fired)
            elapsed = time.monotonic() - started
            # Wait out the remainder of the poll window (short tick = no drift).
            self._stop.wait(max(0.05, self._scheduler._poll_interval - elapsed))
        log.info("stopping; shutting down runtime service")
        try:
            self._service.shutdown()
        except Exception:
            log.exception("runtime service shutdown raised")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes spec-schedule supervisor")
    parser.add_argument("--data-dir", default=os.environ.get("HERMES_CORE_DATA_DIR"),
                        help="Runtime data dir (default: $HERMES_CORE_DATA_DIR or ~/.hermes/data)")
    parser.add_argument("--sheets-dir", default=os.environ.get("HERMES_SPEC_SHEETS_DIR"),
                        help="Spec-sheet directory (default: <data-dir>/spec-sheets)")
    parser.add_argument("--poll", type=float,
                        default=float(os.environ.get("HERMES_SPEC_POLL_INTERVAL", "2.0")),
                        help="Tick interval in seconds (default 2.0)")
    parser.add_argument("--log-file", default=os.environ.get("HERMES_SPEC_SUPERVISOR_LOG"),
                        help="Optional log file (in addition to stderr/journal)")
    args = parser.parse_args(argv)

    _setup_logging(args.log_file)

    data_dir = Path(args.data_dir) if args.data_dir else Path.home() / ".hermes" / "data"
    lock_path = data_dir / "spec-supervisor.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                log.error(
                    "another spec-supervisor is already running (lock: %s) - "
                    "exiting 75", lock_path,
                )
                return 75
            raise
        os.ftruncate(lock_fd, 0)
        os.write(lock_fd, f"{os.getpid()}\n".encode())

        sup = Supervisor(args.data_dir, args.sheets_dir, args.poll)
        signal.signal(signal.SIGINT, sup._on_signal)
        signal.signal(signal.SIGTERM, sup._on_signal)
        return sup.run()
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
