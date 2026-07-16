"""Repository layer: the only module that writes SQL/ORM queries for the
`jobs` table. Every other module (cli.py, worker.py) goes through here.
"""
from typing import List, Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import config as config_mod
from . import validators
from .exceptions import DatabaseError, DuplicateJobError, InvalidJobStateError, JobNotFoundError
from .execution import ExecutionResult
from .models import Job, JobLog, State, Worker
from .utils import after_seconds, new_id, utcnow

# --------------------------------------------------------------------------
# Repository CRUD
# --------------------------------------------------------------------------


def create_job(session: Session, data: dict) -> Job:
    command = validators.validate_command(data.get("command"))
    job_id = str(data.get("id") or new_id())
    if job_exists(session, job_id):
        raise DuplicateJobError(f"Job id already exists: {job_id}")

    max_retries = validators.validate_max_retries(
        data.get("max_retries", config_mod.get_int(session, "max_retries"))
    )
    priority = validators.validate_priority(data.get("priority", 0))
    run_at = validators.validate_run_at(data.get("run_at"))
    timeout_seconds = validators.validate_timeout_seconds(data.get("timeout_seconds"))
    now = utcnow()

    job = Job(
        id=job_id,
        command=command,
        state=State.PENDING,
        attempts=0,
        max_retries=max_retries,
        priority=priority,
        run_at=run_at,
        timeout_seconds=timeout_seconds,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    try:
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        raise DatabaseError(f"Failed to create job {job_id}: {exc}") from exc
    return job


def get_job(session: Session, job_id: str) -> Optional[Job]:
    return session.get(Job, job_id)


def job_exists(session: Session, job_id: str) -> bool:
    return session.get(Job, job_id) is not None


def list_jobs(session: Session, state: Optional[str] = None, limit: Optional[int] = None) -> List[Job]:
    query = session.query(Job)
    if state:
        query = query.filter(Job.state == state)
    query = query.order_by(Job.created_at.desc())
    if limit:
        query = query.limit(limit)
    return query.all()


def get_pending_jobs(session: Session, limit: Optional[int] = None) -> List[Job]:
    return list_jobs(session, state=State.PENDING, limit=limit)


_UPDATABLE_FIELDS = {
    "command": validators.validate_command,
    "max_retries": validators.validate_max_retries,
    "priority": validators.validate_priority,
    "run_at": validators.validate_run_at,
    "timeout_seconds": validators.validate_timeout_seconds,
}


def update_job(session: Session, job_id: str, **fields) -> Job:
    """Generic partial update for the fields a caller is allowed to change
    directly. Lifecycle fields (state/attempts/next_retry/worker_id/
    last_error) are intentionally not editable here -- those only change
    through claim_job/complete_job/fail_job/dlq_retry, which encode the
    actual state-machine rules."""
    job = get_job(session, job_id)
    if job is None:
        raise JobNotFoundError(f"Job not found: {job_id}")

    unknown = set(fields) - set(_UPDATABLE_FIELDS)
    if unknown:
        raise ValueError(f"Cannot update field(s): {', '.join(sorted(unknown))}")

    for field, value in fields.items():
        setattr(job, field, _UPDATABLE_FIELDS[field](value))
    job.updated_at = utcnow()

    try:
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        raise DatabaseError(f"Failed to update job {job_id}: {exc}") from exc
    return job


def delete_job(session: Session, job_id: str) -> None:
    job = get_job(session, job_id)
    if job is None:
        raise JobNotFoundError(f"Job not found: {job_id}")
    session.delete(job)
    try:
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        raise DatabaseError(f"Failed to delete job {job_id}: {exc}") from exc


# --------------------------------------------------------------------------
# Job lifecycle (claim -> complete/fail -> DLQ)
# --------------------------------------------------------------------------


def claim_job(session: Session, worker_id: str) -> Optional[Job]:
    """Atomically select and claim one eligible job.

    database._make_engine wires SQLite's BEGIN IMMEDIATE into every
    transaction this session opens, so the SELECT below takes the
    database's write lock before it runs. Two workers racing to claim a
    job are serialized by SQLite itself -- only one of them can ever see
    the job as eligible and move it to 'processing', which is what
    prevents duplicate execution across worker processes.
    """
    now = utcnow()
    job = (
        session.query(Job)
        .filter(
            or_(Job.run_at.is_(None), Job.run_at <= now),
            or_(
                Job.state == State.PENDING,
                and_(Job.state == State.FAILED, Job.next_retry.isnot(None), Job.next_retry <= now),
            ),
        )
        .order_by(Job.priority.desc(), Job.created_at.asc())
        .first()
    )
    if job is None:
        session.commit()  # release the BEGIN IMMEDIATE lock even when idle
        return None

    job.state = State.PROCESSING
    job.worker_id = worker_id
    job.updated_at = now
    session.commit()
    return job


def _log_attempt(
    session: Session,
    job_id: str,
    attempt: int,
    result: ExecutionResult,
    started_at,
    finished_at,
) -> None:
    session.add(
        JobLog(
            job_id=job_id,
            attempt=attempt,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.exit_code,
            started_at=started_at,
            finished_at=finished_at,
        )
    )


def complete_job(session: Session, job: Job, result: ExecutionResult, started_at) -> None:
    now = utcnow()
    job.attempts += 1
    job.state = State.COMPLETED
    job.last_error = None
    job.next_retry = None
    job.updated_at = now
    _log_attempt(session, job.id, job.attempts, result, started_at, now)
    session.commit()


def fail_job(session: Session, job: Job, result: ExecutionResult, started_at, backoff_base: float) -> None:
    now = utcnow()
    job.attempts += 1
    reason = result.stderr.strip() or result.stdout.strip() or f"exit code {result.exit_code}"
    job.last_error = reason[:2000]
    job.updated_at = now

    if job.attempts >= job.max_retries:
        job.state = State.DEAD
        job.next_retry = None
    else:
        job.state = State.FAILED
        job.next_retry = after_seconds(backoff_base ** job.attempts)

    _log_attempt(session, job.id, job.attempts, result, started_at, now)
    session.commit()


def dlq_list(session: Session) -> List[Job]:
    return list_jobs(session, state=State.DEAD)


def dlq_retry(session: Session, job_id: str) -> Job:
    job = get_job(session, job_id)
    if job is None:
        raise JobNotFoundError(f"Job not found: {job_id}")
    if job.state != State.DEAD:
        raise InvalidJobStateError(f"Job {job_id} is not in the DLQ (current state: {job.state})")

    job.state = State.PENDING
    job.attempts = 0
    job.next_retry = None
    job.last_error = None
    job.worker_id = None
    job.updated_at = utcnow()
    session.commit()
    return job


def status_summary(session: Session) -> dict:
    counts = {s: 0 for s in State.ALL}
    for state, count in session.query(Job.state, func.count(Job.id)).group_by(Job.state).all():
        counts[state] = count

    workers = session.query(Worker).order_by(Worker.started_at).all()

    total_attempts = session.query(func.count(JobLog.id)).scalar() or 0
    successes = session.query(func.count(JobLog.id)).filter(JobLog.exit_code == 0).scalar() or 0
    success_rate = round((successes / total_attempts) * 100, 1) if total_attempts else 0.0

    return {
        "jobs_total": sum(counts.values()),
        "by_state": counts,
        "workers": workers,
        "total_attempts_logged": total_attempts,
        "success_rate_pct": success_rate,
    }
