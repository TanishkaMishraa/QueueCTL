from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from queuectl.database import Base
from queuectl.utils import utcnow


class State:
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD = "dead"

    ALL = (PENDING, PROCESSING, COMPLETED, FAILED, DEAD)


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String, primary_key=True)
    command = Column(String, nullable=False)
    state = Column(String, nullable=False, default=State.PENDING)
    attempts = Column(Integer, default=0, nullable=False)
    max_retries = Column(Integer, default=3, nullable=False)

    # Bonus-feature columns beyond the minimal spec: priority queues,
    # run_at scheduling/delayed jobs, and per-job timeout handling.
    priority = Column(Integer, default=0, nullable=False)
    run_at = Column(DateTime, nullable=True)
    timeout_seconds = Column(Integer, nullable=True)
    worker_id = Column(String, nullable=True)
    last_error = Column(Text, nullable=True)

    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    next_retry = Column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "command": self.command,
            "state": self.state,
            "attempts": self.attempts,
            "max_retries": self.max_retries,
            "priority": self.priority,
            "run_at": self.run_at,
            "next_retry": self.next_retry,
            "timeout_seconds": self.timeout_seconds,
            "worker_id": self.worker_id,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class Config(Base):
    __tablename__ = "config"

    key = Column(String, primary_key=True)
    value = Column(String, nullable=False)


class Worker(Base):
    """Tracks each worker process so `status`/`worker stop` work across
    separate CLI invocations (not part of the tutorial's 2-table schema,
    but required for the assignment's active-worker reporting and
    graceful-shutdown requirements)."""

    __tablename__ = "workers"

    worker_id = Column(String, primary_key=True)
    pid = Column(Integer, nullable=True)
    status = Column(String, nullable=False)
    stop_requested = Column(Boolean, default=False, nullable=False)
    current_job_id = Column(String, nullable=True)
    started_at = Column(DateTime, default=utcnow, nullable=False)
    last_heartbeat = Column(DateTime, default=utcnow, nullable=False)


class JobLog(Base):
    """One row per execution attempt (bonus: job output logging / stats)."""

    __tablename__ = "job_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String, nullable=False)
    attempt = Column(Integer, nullable=False)
    stdout = Column(Text, nullable=True)
    stderr = Column(Text, nullable=True)
    exit_code = Column(Integer, nullable=False)
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime, nullable=False)
