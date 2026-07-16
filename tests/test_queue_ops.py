import threading

import pytest

from queuectl import db as db_mod
from queuectl import queue_ops
from queuectl.execution import ExecutionResult
from queuectl.models import State


def test_enqueue_sets_defaults(conn):
    job = queue_ops.enqueue_job(conn, {"command": "echo hi"})
    assert job.state == State.PENDING
    assert job.attempts == 0
    assert job.max_retries == 3  # default from config
    assert job.command == "echo hi"


def test_enqueue_requires_command(conn):
    with pytest.raises(ValueError):
        queue_ops.enqueue_job(conn, {"id": "job1"})


def test_enqueue_rejects_duplicate_id(conn):
    queue_ops.enqueue_job(conn, {"id": "job1", "command": "echo hi"})
    with pytest.raises(ValueError):
        queue_ops.enqueue_job(conn, {"id": "job1", "command": "echo again"})


def test_claim_job_returns_none_when_empty(conn):
    assert queue_ops.claim_job(conn, "w1") is None


def test_claim_job_marks_processing(conn):
    queue_ops.enqueue_job(conn, {"id": "job1", "command": "echo hi"})
    job = queue_ops.claim_job(conn, "w1")
    assert job is not None
    assert job.state == State.PROCESSING
    assert job.worker_id == "w1"
    assert queue_ops.claim_job(conn, "w2") is None  # already claimed


def test_complete_job_transitions_to_completed(conn):
    queue_ops.enqueue_job(conn, {"id": "job1", "command": "echo hi"})
    job = queue_ops.claim_job(conn, "w1")
    result = ExecutionResult(exit_code=0, stdout="hi\n", stderr="")
    queue_ops.complete_job(conn, job, result, started_at="now")
    updated = queue_ops.get_job(conn, "job1")
    assert updated.state == State.COMPLETED
    assert updated.attempts == 1


def test_fail_job_retries_then_moves_to_dlq(conn):
    queue_ops.enqueue_job(conn, {"id": "job1", "command": "exit 1", "max_retries": 2})
    result = ExecutionResult(exit_code=1, stdout="", stderr="boom")

    job = queue_ops.claim_job(conn, "w1")
    queue_ops.fail_job(conn, job, result, started_at="now", backoff_base=2)
    job = queue_ops.get_job(conn, "job1")
    assert job.state == State.FAILED
    assert job.attempts == 1
    assert job.next_attempt_at is not None

    # Second failure reaches max_retries -> DLQ
    job.state = State.PENDING  # simulate becoming eligible again (bypassing real delay)
    queue_ops.fail_job(conn, job, result, started_at="now", backoff_base=2)
    job = queue_ops.get_job(conn, "job1")
    assert job.state == State.DEAD
    assert job.attempts == 2
    assert job.last_error == "boom"


def test_dlq_retry_resets_job(conn):
    queue_ops.enqueue_job(conn, {"id": "job1", "command": "exit 1", "max_retries": 1})
    result = ExecutionResult(exit_code=1, stdout="", stderr="boom")
    job = queue_ops.claim_job(conn, "w1")
    queue_ops.fail_job(conn, job, result, started_at="now", backoff_base=2)
    dead = queue_ops.get_job(conn, "job1")
    assert dead.state == State.DEAD

    retried = queue_ops.dlq_retry(conn, "job1")
    assert retried.state == State.PENDING
    assert retried.attempts == 0


def test_dlq_retry_rejects_non_dead_job(conn):
    queue_ops.enqueue_job(conn, {"id": "job1", "command": "echo hi"})
    with pytest.raises(ValueError):
        queue_ops.dlq_retry(conn, "job1")


def test_list_jobs_filters_by_state(conn):
    queue_ops.enqueue_job(conn, {"id": "job1", "command": "echo hi"})
    queue_ops.enqueue_job(conn, {"id": "job2", "command": "echo hi"})
    queue_ops.claim_job(conn, "w1")
    pending = queue_ops.list_jobs(conn, state=State.PENDING)
    processing = queue_ops.list_jobs(conn, state=State.PROCESSING)
    assert len(pending) == 1
    assert len(processing) == 1


def test_concurrent_claims_never_double_claim(db_path):
    conn = db_mod.connect(db_path)
    for i in range(20):
        queue_ops.enqueue_job(conn, {"id": f"job{i}", "command": "echo hi"})
    conn.close()

    claimed = []
    lock = threading.Lock()

    def worker_loop():
        thread_conn = db_mod.connect(db_path)
        try:
            while True:
                job = queue_ops.claim_job(thread_conn, threading.current_thread().name)
                if job is None:
                    break
                with lock:
                    claimed.append(job.id)
        finally:
            thread_conn.close()

    threads = [threading.Thread(target=worker_loop) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == 20
    assert len(set(claimed)) == 20  # no duplicates
