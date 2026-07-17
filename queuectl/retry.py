"""Pure retry/backoff math -- deliberately independent of the database and
the ORM so the exact delay sequence and dead/retry decision are trivial to
unit-test (see tests/test_retry.py) without a session fixture.

queue_ops.fail_job is what actually applies these decisions to a Job; this
module only computes numbers.
"""


def calculate_delay(attempts: int, base: float) -> float:
    """Exponential backoff delay in seconds after `attempts` failed
    attempts: base ** attempts. With base=2, attempts 1/2/3/4 give
    2/4/8/16 seconds."""
    return base**attempts


def is_dead(attempts: int, max_retries: int) -> bool:
    """A job is permanently failed (DLQ-bound) once every allowed attempt
    has been used."""
    return attempts >= max_retries
