from dataclasses import dataclass
from typing import Optional


class State:
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD = "dead"

    ALL = (PENDING, PROCESSING, COMPLETED, FAILED, DEAD)


@dataclass
class Job:
    id: str
    command: str
    state: str
    attempts: int
    max_retries: int
    priority: int
    run_at: Optional[str]
    next_attempt_at: Optional[str]
    timeout_seconds: Optional[int]
    worker_id: Optional[str]
    last_error: Optional[str]
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row) -> "Job":
        return cls(**{k: row[k] for k in row.keys()})

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "command": self.command,
            "state": self.state,
            "attempts": self.attempts,
            "max_retries": self.max_retries,
            "priority": self.priority,
            "run_at": self.run_at,
            "next_attempt_at": self.next_attempt_at,
            "timeout_seconds": self.timeout_seconds,
            "worker_id": self.worker_id,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
