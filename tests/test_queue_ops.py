import threading

import pytest

from queuectl import database
from queuectl import queue_ops
from queuectl.exceptions import (
    DuplicateJobError,
    InvalidJobDataError,
    InvalidJobStateError,
    JobNotFoundError,
)
from queuectl.executor import ExecutionResult
from queuectl.models import State
from queuectl.utils import utcnow


def test_enqueue_sets_defaults(session):
    job = queue_ops.create_job(session, {"command": "echo hi"})
    assert job.state == State.PENDING
    assert job.attempts == 0
    assert job.max_retries == 3  # default from config
    assert job.command == "echo hi"


def test_enqueue_requires_command(session):
    with pytest.raises(InvalidJobDataError):
        queue_ops.create_job(session, {"id": "job1"})


def test_enqueue_rejects_duplicate_id(session):
    queue_ops.create_job(session, {"id": "job1", "command": "echo hi"})
    with pytest.raises(DuplicateJobError):
        queue_ops.create_job(session, {"id": "job1", "command": "echo again"})


def test_enqueue_rejects_negative_max_retries(session):
    with pytest.raises(InvalidJobDataError):
        queue_ops.create_job(session, {"command": "echo hi", "max_retries": -1})


def test_enqueue_rejects_negative_priority(session):
    with pytest.raises(InvalidJobDataError):
        queue_ops.create_job(session, {"command": "echo hi", "priority": -1})


def test_claim_job_returns_none_when_empty(session):
    assert queue_ops.claim_job(session, "w1") is None


def test_claim_job_marks_processing(session):
    queue_ops.create_job(session, {"id": "job1", "command": "echo hi"})
    job = queue_ops.claim_job(session, "w1")
    assert job is not None
    assert job.state == State.PROCESSING
    assert job.worker_id == "w1"
    assert queue_ops.claim_job(session, "w2") is None  # already claimed


def test_complete_job_transitions_to_completed(session):
    queue_ops.create_job(session, {"id": "job1", "command": "echo hi"})
    job = queue_ops.claim_job(session, "w1")
    result = ExecutionResult(exit_code=0, stdout="hi\n", stderr="")
    queue_ops.complete_job(session, job, result, started_at=utcnow())
    updated = queue_ops.get_job(session, "job1")
    assert updated.state == State.COMPLETED
    assert updated.attempts == 1


def test_fail_job_retries_then_moves_to_dlq(session):
    queue_ops.create_job(session, {"id": "job1", "command": "exit 1", "max_retries": 2})
    result = ExecutionResult(exit_code=1, stdout="", stderr="boom")

    job = queue_ops.claim_job(session, "w1")
    queue_ops.fail_job(session, job, result, started_at=utcnow(), backoff_base=2)
    job = queue_ops.get_job(session, "job1")
    assert job.state == State.FAILED
    assert job.attempts == 1
    assert job.next_retry is not None

    # Second failure reaches max_retries -> DLQ
    job.state = State.PENDING  # simulate becoming eligible again (bypassing real delay)
    queue_ops.fail_job(session, job, result, started_at=utcnow(), backoff_base=2)
    job = queue_ops.get_job(session, "job1")
    assert job.state == State.DEAD
    assert job.attempts == 2
    assert job.last_error == "boom"


def test_dlq_retry_resets_job(session):
    queue_ops.create_job(session, {"id": "job1", "command": "exit 1", "max_retries": 1})
    result = ExecutionResult(exit_code=1, stdout="", stderr="boom")
    job = queue_ops.claim_job(session, "w1")
    queue_ops.fail_job(session, job, result, started_at=utcnow(), backoff_base=2)
    dead = queue_ops.get_job(session, "job1")
    assert dead.state == State.DEAD

    retried = queue_ops.dlq_retry(session, "job1")
    assert retried.state == State.PENDING
    assert retried.attempts == 0


def test_dlq_retry_rejects_non_dead_job(session):
    queue_ops.create_job(session, {"id": "job1", "command": "echo hi"})
    with pytest.raises(InvalidJobStateError):
        queue_ops.dlq_retry(session, "job1")


def test_job_exists(session):
    assert queue_ops.job_exists(session, "job1") is False
    queue_ops.create_job(session, {"id": "job1", "command": "echo hi"})
    assert queue_ops.job_exists(session, "job1") is True


def test_get_pending_jobs(session):
    queue_ops.create_job(session, {"id": "job1", "command": "echo hi"})
    queue_ops.create_job(session, {"id": "job2", "command": "echo hi"})
    queue_ops.claim_job(session, "w1")
    pending = queue_ops.get_pending_jobs(session)
    assert [job.id for job in pending] == ["job2"]


def test_update_job_changes_allowed_fields(session):
    queue_ops.create_job(session, {"id": "job1", "command": "echo hi"})
    updated = queue_ops.update_job(session, "job1", command="echo bye", priority=5)
    assert updated.command == "echo bye"
    assert updated.priority == 5


def test_update_job_rejects_unknown_field(session):
    queue_ops.create_job(session, {"id": "job1", "command": "echo hi"})
    with pytest.raises(ValueError):
        queue_ops.update_job(session, "job1", state="dead")


def test_update_job_missing_raises(session):
    with pytest.raises(JobNotFoundError):
        queue_ops.update_job(session, "nope", priority=1)


def test_delete_job_removes_row(session):
    queue_ops.create_job(session, {"id": "job1", "command": "echo hi"})
    queue_ops.delete_job(session, "job1")
    assert queue_ops.get_job(session, "job1") is None


def test_delete_job_missing_raises(session):
    with pytest.raises(JobNotFoundError):
        queue_ops.delete_job(session, "nope")


def test_list_jobs_filters_by_state(session):
    queue_ops.create_job(session, {"id": "job1", "command": "echo hi"})
    queue_ops.create_job(session, {"id": "job2", "command": "echo hi"})
    queue_ops.claim_job(session, "w1")
    pending = queue_ops.list_jobs(session, state=State.PENDING)
    processing = queue_ops.list_jobs(session, state=State.PROCESSING)
    assert len(pending) == 1
    assert len(processing) == 1


def test_concurrent_claims_never_double_claim(db_path):
    session = database.get_session(db_path)
    for i in range(20):
        queue_ops.create_job(session, {"id": f"job{i}", "command": "echo hi"})
    session.close()

    claimed = []
    lock = threading.Lock()

    def worker_loop():
        thread_session = database.get_session(db_path)
        try:
            while True:
                job = queue_ops.claim_job(thread_session, threading.current_thread().name)
                if job is None:
                    break
                with lock:
                    claimed.append(job.id)
        finally:
            thread_session.close()

    threads = [threading.Thread(target=worker_loop) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == 20
    assert len(set(claimed)) == 20  # no duplicates
