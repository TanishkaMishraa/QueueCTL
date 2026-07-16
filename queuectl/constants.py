"""Centralized state names and default configuration values.

Every other module (models.py, config.py, queue_ops.py) reads these
constants rather than repeating string/number literals, so there is one
place to change a state name or a default.
"""

PENDING = "pending"
PROCESSING = "processing"
COMPLETED = "completed"
FAILED = "failed"
DEAD = "dead"

ALL_STATES = (PENDING, PROCESSING, COMPLETED, FAILED, DEAD)

DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_BASE = 2
DEFAULT_TIMEOUT = 30
DEFAULT_PRIORITY = 0
DEFAULT_POLL_INTERVAL = 1
DEFAULT_HEARTBEAT_INTERVAL = 2
