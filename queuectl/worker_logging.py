"""Worker lifecycle logging: appends human-readable events (started,
picked a job, executing, completed, failed/retry, moved to DLQ, stopped)
to logs/worker.log.

This is separate from the job_logs database table -- job_logs stores
structured per-attempt stdout/stderr/exit code for the job output logging
bonus feature; this file is a plain operational log of *worker* activity,
the kind you'd tail while a queue is running.
"""
import logging
import os
from pathlib import Path

_LOGGER_NAME = "queuectl.worker"


def get_log_dir() -> Path:
    override = os.environ.get("QUEUECTL_LOG_DIR")
    path = Path(override) if override else Path.cwd() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_worker_logger() -> logging.Logger:
    """Returns a logger writing to logs/worker.log. Each worker process
    calls this once at startup; handlers are only attached the first time
    per-process, so repeated calls are safe."""
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        handler = logging.FileHandler(get_log_dir() / "worker.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
