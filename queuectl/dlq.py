"""DLQ-scoped repository operations: list, count, retry, and delete dead
jobs. This module exists to give DLQ operations their own dedicated
names/namespace (so cli.py's `dlq` command group reads as `dlq.list_dead_jobs()`,
`dlq.retry_dead_job()`, etc.) without becoming a second place that talks
to the database directly -- every function here delegates the actual
Session.query/add/delete work to queue_ops.py, which remains the only
module that does that.
"""

from typing import List, Optional

from sqlalchemy.orm import Session

from . import queue_ops
from .exceptions import InvalidJobStateError, JobNotFoundError
from .models import Job, State


def list_dead_jobs(session: Session, limit: Optional[int] = None) -> List[Job]:
    return queue_ops.list_jobs(session, state=State.DEAD, limit=limit)


def count_dead_jobs(session: Session) -> int:
    return queue_ops.count_jobs(session, state=State.DEAD)


def retry_dead_job(session: Session, job_id: str) -> Job:
    return queue_ops.dlq_retry(session, job_id)


def delete_dead_job(session: Session, job_id: str) -> None:
    """Delete a job, but only if it's actually in the DLQ -- unlike
    queue_ops.delete_job (generic CRUD, any state), this refuses to
    delete a job that's still pending/processing/etc, since that's not
    what `queuectl dlq delete` should be able to do."""
    job = queue_ops.get_job(session, job_id)
    if job is None:
        raise JobNotFoundError(f"Job not found: {job_id}")
    if job.state != State.DEAD:
        raise InvalidJobStateError(f"Job {job_id} is not in the DLQ (current state: {job.state})")
    queue_ops.delete_job(session, job_id)
