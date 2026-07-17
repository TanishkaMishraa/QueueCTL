"""Derived queue metrics: execution-time stats and success/failure/retry
rates, computed from data queue_ops.py already fetches. This module holds
no ORM queries of its own -- consistent with queue_ops.py being the only
module that touches Session directly (see dlq.py for the same pattern).
"""
from . import queue_ops
from .models import State


def calculate_metrics(session) -> dict:
    logs = queue_ops.list_job_logs(session)
    completed_jobs = queue_ops.count_jobs(session, state=State.COMPLETED)
    dead_jobs = queue_ops.count_jobs(session, state=State.DEAD)

    total_attempts = len(logs)
    failed_attempts = sum(1 for log in logs if log.exit_code != 0)
    retry_attempts = sum(1 for log in logs if log.attempt > 1)

    durations = [(log.finished_at - log.started_at).total_seconds() for log in logs]
    avg_runtime = sum(durations) / len(durations) if durations else 0.0
    longest_runtime = max(durations) if durations else 0.0
    shortest_runtime = min(durations) if durations else 0.0

    success_rate = round((total_attempts - failed_attempts) / total_attempts * 100, 1) if total_attempts else 0.0
    failure_rate = round(failed_attempts / total_attempts * 100, 1) if total_attempts else 0.0
    retry_rate = round(retry_attempts / total_attempts * 100, 1) if total_attempts else 0.0

    # Throughput is inherently a judgment call for a short-lived local
    # queue. Extrapolating a sub-minute span out to an hourly rate
    # produces a meaningless (often huge) number, so below a 60-second
    # span we just report the raw completed count instead of a rate.
    span_seconds = (logs[-1].finished_at - logs[0].started_at).total_seconds() if len(logs) >= 2 else 0
    if span_seconds >= 60:
        throughput_per_hour = completed_jobs / (span_seconds / 3600)
    else:
        throughput_per_hour = float(completed_jobs)

    return {
        "completed_jobs": completed_jobs,
        "dead_jobs": dead_jobs,
        "total_attempts": total_attempts,
        "failed_attempts": failed_attempts,
        "retry_attempts": retry_attempts,
        "avg_runtime_seconds": round(avg_runtime, 3),
        "longest_runtime_seconds": round(longest_runtime, 3),
        "shortest_runtime_seconds": round(shortest_runtime, 3),
        "success_rate_pct": success_rate,
        "failure_rate_pct": failure_rate,
        "retry_rate_pct": retry_rate,
        "throughput_per_hour": round(throughput_per_hour, 2),
    }
