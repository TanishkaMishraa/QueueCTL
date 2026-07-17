"""Direct tests of the worker.run() loop against a real SQLite file, run
in a background thread rather than a real subprocess (scripts/validate_e2e.py
already covers the real-subprocess, real-concurrency case). Signal
registration is patched out because signal.signal() only works from the
main thread, which this test deliberately isn't -- nothing here exercises
signal handling anyway; that's covered by validate_e2e.py's graceful
shutdown scenario against a real process.
"""

import sys
import threading
import time
from unittest.mock import patch

from queuectl import database, queue_ops, worker
from queuectl.models import State, Worker

PYTHON = sys.executable


def _run_worker_and_wait(db_path, worker_id, condition, timeout=10.0):
    """Run worker.run(worker_id) in a background thread until `condition()`
    is true (polled via a fresh session each time), then request it to
    stop and join the thread."""
    with patch("queuectl.worker._register_signal_handlers"):
        thread = threading.Thread(target=worker.run, args=(worker_id,), daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + timeout
            satisfied = False
            while time.monotonic() < deadline:
                check_session = database.get_session(db_path)
                try:
                    satisfied = condition(check_session)
                finally:
                    check_session.close()
                if satisfied:
                    break
                time.sleep(0.2)
        finally:
            stop_session = database.get_session(db_path)
            try:
                stop_session.query(Worker).update({"stop_requested": True})
                stop_session.commit()
            finally:
                stop_session.close()
            thread.join(timeout=10)
        return satisfied, thread


def test_worker_claims_executes_and_completes_a_job(db_path):
    session = database.get_session(db_path)
    queue_ops.create_job(session, {"id": "job1", "command": f'{PYTHON} -c "print(1)"'})
    session.close()

    satisfied, thread = _run_worker_and_wait(
        db_path, "w1", lambda s: queue_ops.get_job(s, "job1").state == State.COMPLETED
    )

    assert satisfied, "job1 never reached 'completed'"
    assert not thread.is_alive()

    session = database.get_session(db_path)
    worker_row = session.get(Worker, "w1")
    assert worker_row.status == "stopped"  # own shutdown code ran cleanly
    session.close()


def test_worker_retries_then_moves_failing_job_to_dlq(db_path):
    session = database.get_session(db_path)
    from queuectl import config as config_mod

    config_mod.set_config(session, "backoff_base", "1.1")  # must be > 1; keeps the retry wait short
    queue_ops.create_job(
        session, {"id": "job2", "command": f'{PYTHON} -c "import sys; sys.exit(1)"', "max_retries": 2}
    )
    session.close()

    satisfied, thread = _run_worker_and_wait(
        db_path, "w2", lambda s: queue_ops.get_job(s, "job2").state == State.DEAD, timeout=15.0
    )

    assert satisfied, "job2 never reached the DLQ"
    assert not thread.is_alive()

    session = database.get_session(db_path)
    job = queue_ops.get_job(session, "job2")
    assert job.attempts == 2
    session.close()


def test_worker_idles_without_error_when_queue_is_empty(db_path):
    session = database.get_session(db_path)
    from queuectl import config as config_mod

    config_mod.set_config(session, "poll_interval", "0.1")
    session.close()

    with patch("queuectl.worker._register_signal_handlers"):
        thread = threading.Thread(target=worker.run, args=("w3",), daemon=True)
        thread.start()
        time.sleep(0.5)  # a few idle poll cycles with nothing to claim

        stop_session = database.get_session(db_path)
        stop_session.query(Worker).update({"stop_requested": True})
        stop_session.commit()
        stop_session.close()
        thread.join(timeout=10)

    assert not thread.is_alive()
    session = database.get_session(db_path)
    worker_row = session.get(Worker, "w3")
    assert worker_row.status == "stopped"
    session.close()
