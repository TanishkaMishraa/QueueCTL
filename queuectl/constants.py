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
DEFAULT_MAX_WORKERS = 10

# A worker whose last_heartbeat is older than this is considered dead/stuck
# (crashed, killed, hung) even though its `workers` row still says
# status='running' -- nothing marks it stopped, since that only happens in
# the worker's own shutdown code, which a crash never reaches.
HEARTBEAT_STALE_SECONDS = 30
