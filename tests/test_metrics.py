from queuectl import metrics, queue_ops
from queuectl.executor import ExecutionResult
from queuectl.utils import utcnow


def _run_job(session, job_id, exit_code, max_retries=3):
    queue_ops.create_job(session, {"id": job_id, "command": "irrelevant", "max_retries": max_retries})
    job = queue_ops.claim_job(session, "w1")
    result = ExecutionResult(exit_code=exit_code, stdout="", stderr="boom" if exit_code else "")
    started = utcnow()
    if exit_code == 0:
        queue_ops.complete_job(session, job, result, started)
    else:
        queue_ops.fail_job(session, job, result, started, backoff_base=2)
    return job


def test_calculate_metrics_empty_queue(session):
    m = metrics.calculate_metrics(session)
    assert m["completed_jobs"] == 0
    assert m["dead_jobs"] == 0
    assert m["total_attempts"] == 0
    assert m["success_rate_pct"] == 0.0
    assert m["throughput_per_hour"] == 0.0


def test_calculate_metrics_counts_completed_and_dead(session):
    _run_job(session, "ok1", exit_code=0)
    _run_job(session, "bad1", exit_code=1, max_retries=1)

    m = metrics.calculate_metrics(session)
    assert m["completed_jobs"] == 1
    assert m["dead_jobs"] == 1
    assert m["total_attempts"] == 2
    assert m["failed_attempts"] == 1
    assert m["success_rate_pct"] == 50.0
    assert m["failure_rate_pct"] == 50.0


def test_calculate_metrics_counts_retries(session):
    # max_retries=2: first failure schedules a retry (attempt 1, not yet
    # dead), second failure exhausts retries (attempt 2, dead). The
    # second logged attempt is the only one with attempt > 1.
    queue_ops.create_job(session, {"id": "job1", "command": "exit 1", "max_retries": 2})
    result = ExecutionResult(exit_code=1, stdout="", stderr="boom")
    job = queue_ops.claim_job(session, "w1")
    queue_ops.fail_job(session, job, result, utcnow(), backoff_base=2)
    job.state = "pending"  # bypass the real backoff delay for the test
    queue_ops.fail_job(session, job, result, utcnow(), backoff_base=2)

    m = metrics.calculate_metrics(session)
    assert m["total_attempts"] == 2
    assert m["retry_attempts"] == 1


def test_calculate_metrics_runtime_stats(session):
    queue_ops.create_job(session, {"id": "job1", "command": "irrelevant"})
    job = queue_ops.claim_job(session, "w1")
    result = ExecutionResult(exit_code=0, stdout="", stderr="", duration_seconds=1.0)
    started = utcnow()
    queue_ops.complete_job(session, job, result, started)

    m = metrics.calculate_metrics(session)
    assert m["avg_runtime_seconds"] >= 0.0
    assert m["longest_runtime_seconds"] >= m["shortest_runtime_seconds"]
