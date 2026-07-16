class QueueCTLError(Exception):
    """Base class for every queuectl-specific error."""


class JobNotFoundError(QueueCTLError):
    """Raised when a job id doesn't exist."""


class DuplicateJobError(QueueCTLError):
    """Raised when enqueuing a job id that already exists."""


class InvalidJobDataError(QueueCTLError):
    """Raised when job input fails validation (validators.py)."""


class InvalidJobStateError(QueueCTLError):
    """Raised when an operation is attempted from the wrong job state,
    e.g. retrying a job that isn't in the DLQ."""


class InvalidConfiguration(QueueCTLError):
    """Raised for unknown config keys or invalid config values."""


class DatabaseError(QueueCTLError):
    """Raised when an unexpected database/SQLAlchemy failure occurs."""
