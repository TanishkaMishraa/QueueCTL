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
from . import database, queue_ops, retry
from .app_logging import get_worker_logger
from .executor import run_command
from .models import State, Worker
from .utils import new_id, utcnow

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


def run(worker_id: str = None) -> None:
    _register_signal_handlers()
    session = database.get_session()
    worker_id = worker_id or new_id()
    now = utcnow()
    logger = get_worker_logger()

    worker_row = Worker(
        worker_id=worker_id,
        pid=os.getpid(),
        status="running",
        stop_requested=False,
        current_job_id=None,
        started_at=now,
        last_heartbeat=now,
    )
    session.add(worker_row)
    session.commit()
    logger.info(f"[{worker_id}] Worker started (pid={os.getpid()})")

    try:
        # Each loop iteration re-reads worker_row.stop_requested: the
        # session's expire-on-commit behaviour means this is a fresh read
        # from the database every time, so a stop_requested flag set by a
        # different process (`worker stop`) is picked up promptly.
        while not _stop_requested and not worker_row.stop_requested:
            poll_interval = config_mod.get_float(session, "poll_interval")
            backoff_base = config_mod.get_float(session, "backoff_base")

            job = queue_ops.claim_job(session, worker_id)
            if job is None:
                worker_row.last_heartbeat = utcnow()
                session.commit()
                time.sleep(poll_interval)
                continue

            logger.info(f"[{worker_id}] Picked job {job.id}: {job.command!r}")
            worker_row.current_job_id = job.id
            worker_row.last_heartbeat = utcnow()
            session.commit()

            started_at = utcnow()
            result = run_command(job.command, job.timeout_seconds)

            if result.exit_code == 0:
                queue_ops.complete_job(session, job, result, started_at)
                logger.info(f"[{worker_id}] Job {job.id} completed in {result.duration_seconds:.2f}s")
            else:
                queue_ops.fail_job(session, job, result, started_at, backoff_base)
                if job.state == State.DEAD:
                    # ERROR level: this is the event error.log exists to
                    # surface -- a job has permanently failed.
                    logger.error(
                        f"[{worker_id}] Job {job.id} exceeded retries ({job.attempts}/{job.max_retries}), moved to DLQ"
                    )
                else:
                    delay = retry.calculate_delay(job.attempts, backoff_base)
                    logger.warning(
                        f"[{worker_id}] Job {job.id} failed (attempt {job.attempts}/{job.max_retries}), "
                        f"retry in {delay:.0f}s"
                    )

            worker_row.current_job_id = None
            worker_row.last_heartbeat = utcnow()
            session.commit()
    finally:
        worker_row.status = "stopped"
        worker_row.stop_requested = False
        worker_row.current_job_id = None
        worker_row.last_heartbeat = utcnow()
        session.commit()
        session.close()
        logger.info(f"[{worker_id}] Worker stopped")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else None)
