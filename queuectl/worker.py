"""Worker process entry point.

Each worker runs this loop as its own OS process (spawned by
worker_manager.start_workers). It repeatedly claims one job at a time,
executes it, and records the outcome. It never picks up a new job once a
stop has been requested -- it always finishes the job currently in hand
first, which is what makes shutdown graceful.
"""
import os
import signal
import sys
import time

from . import config as config_mod
from . import db
from . import queue_ops
from .execution import run_command
from .utils import new_id, now_iso

_stop_requested = False


def _handle_signal(signum, frame):
    global _stop_requested
    _stop_requested = True


def _register_signal_handlers():
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (AttributeError, ValueError):
        pass


def _db_stop_requested(conn, worker_id: str) -> bool:
    row = conn.execute(
        "SELECT stop_requested FROM workers WHERE worker_id = ?", (worker_id,)
    ).fetchone()
    return bool(row and row["stop_requested"])


def run(worker_id: str = None) -> None:
    _register_signal_handlers()
    conn = db.connect()
    worker_id = worker_id or new_id()
    pid = os.getpid()
    now = now_iso()

    conn.execute(
        """
        INSERT INTO workers (worker_id, pid, status, stop_requested, current_job_id, started_at, last_heartbeat)
        VALUES (?, ?, 'running', 0, NULL, ?, ?)
        """,
        (worker_id, pid, now, now),
    )

    try:
        while not _stop_requested and not _db_stop_requested(conn, worker_id):
            poll_interval = config_mod.get_float(conn, "poll_interval")
            backoff_base = config_mod.get_float(conn, "backoff_base")

            job = queue_ops.claim_job(conn, worker_id)
            if job is None:
                conn.execute(
                    "UPDATE workers SET last_heartbeat = ? WHERE worker_id = ?",
                    (now_iso(), worker_id),
                )
                time.sleep(poll_interval)
                continue

            conn.execute(
                "UPDATE workers SET current_job_id = ?, last_heartbeat = ? WHERE worker_id = ?",
                (job.id, now_iso(), worker_id),
            )

            started_at = now_iso()
            result = run_command(job.command, job.timeout_seconds)

            if result.exit_code == 0:
                queue_ops.complete_job(conn, job, result, started_at)
            else:
                queue_ops.fail_job(conn, job, result, started_at, backoff_base)

            conn.execute(
                "UPDATE workers SET current_job_id = NULL, last_heartbeat = ? WHERE worker_id = ?",
                (now_iso(), worker_id),
            )
    finally:
        conn.execute(
            "UPDATE workers SET status = 'stopped', stop_requested = 0, current_job_id = NULL, "
            "last_heartbeat = ? WHERE worker_id = ?",
            (now_iso(), worker_id),
        )
        conn.close()


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
