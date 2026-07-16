import pytest

from queuectl import dlq, queue_ops
from queuectl.exceptions import InvalidJobStateError, JobNotFoundError
from queuectl.executor import ExecutionResult
from queuectl.models import State
from queuectl.utils import utcnow


def _make_dead_job(session, job_id="job1", max_retries=1):
    queue_ops.create_job(session, {"id": job_id, "command": "exit 1", "max_retries": max_retries})
    result = ExecutionResult(exit_code=1, stdout="", stderr="boom")
    job = queue_ops.claim_job(session, "w1")
    queue_ops.fail_job(session, job, result, started_at=utcnow(), backoff_base=2)
    return queue_ops.get_job(session, job_id)


def test_list_dead_jobs_only_returns_dead(session):
    _make_dead_job(session, "dead1")
    queue_ops.create_job(session, {"id": "alive1", "command": "echo hi"})

    dead = dlq.list_dead_jobs(session)
    assert [j.id for j in dead] == ["dead1"]


def test_count_dead_jobs(session):
    assert dlq.count_dead_jobs(session) == 0
    _make_dead_job(session, "dead1")
    _make_dead_job(session, "dead2")
    assert dlq.count_dead_jobs(session) == 2


def test_retry_dead_job_resets_to_pending(session):
    _make_dead_job(session, "dead1")
    retried = dlq.retry_dead_job(session, "dead1")
    assert retried.state == State.PENDING
    assert retried.attempts == 0
    assert retried.next_retry is None


def test_retry_rejects_non_dead_job(session):
    queue_ops.create_job(session, {"id": "job1", "command": "echo hi"})
    with pytest.raises(InvalidJobStateError):
        dlq.retry_dead_job(session, "job1")


def test_delete_dead_job_removes_it(session):
    _make_dead_job(session, "dead1")
    dlq.delete_dead_job(session, "dead1")
    assert queue_ops.get_job(session, "dead1") is None


def test_delete_dead_job_rejects_non_dead_job(session):
    queue_ops.create_job(session, {"id": "job1", "command": "echo hi"})
    with pytest.raises(InvalidJobStateError):
        dlq.delete_dead_job(session, "job1")
    # still there -- rejected delete must not have removed it
    assert queue_ops.get_job(session, "job1") is not None


def test_delete_dead_job_missing_raises(session):
    with pytest.raises(JobNotFoundError):
        dlq.delete_dead_job(session, "nope")


def test_workers_never_claim_a_dead_job(session):
    _make_dead_job(session, "dead1")
    # The only other job available is also the same dead one; claim_job
    # must return None rather than ever handing back a dead job.
    assert queue_ops.claim_job(session, "w2") is None
