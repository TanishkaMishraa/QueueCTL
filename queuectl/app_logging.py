"""Centralized logging setup, writing to three files under logs/:

- worker.log  -- worker process lifecycle (started, picked a job,
  completed, failed/retry, moved to DLQ, stopped).
- queuectl.log -- general CLI-driven operational events (job created,
  job deleted, config changed, DLQ manually retried/deleted).
- error.log   -- every ERROR-level event logged anywhere, funneled into
  one place for quick triage, without a second explicit log call.

This is separate from the job_logs database table -- job_logs stores
structured per-attempt stdout/stderr/exit code for the job output logging
bonus feature; these are plain text operational logs you'd tail while a
queue is running.

The error.log behavior comes from Python's logging hierarchy rather than
manual duplication: get_app_logger()/get_worker_logger() return children
of a "queuectl" root logger. A child logger's records propagate up to the
root by default, and the root's own handler is filtered to level=ERROR,
so an .error(...) call on either child is written to that child's own
file *and* to error.log, while .info()/.warning() calls only reach the
child's file (they still propagate, but the root handler's level filter
drops them before they'd be written to error.log).
"""

import logging
import os
from pathlib import Path

_ROOT_NAME = "queuectl"


def get_log_dir() -> Path:
    override = os.environ.get("QUEUECTL_LOG_DIR")
    path = Path(override) if override else Path.cwd() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _add_file_handler(logger: logging.Logger, filename: str, level: int) -> None:
    target = str(get_log_dir() / filename)
    already_attached = any(
        isinstance(h, logging.FileHandler) and h.baseFilename == os.path.abspath(target)
        for h in logger.handlers
    )
    if already_attached:
        return
    handler = logging.FileHandler(target, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)


def get_root_logger() -> logging.Logger:
    root = logging.getLogger(_ROOT_NAME)
    root.setLevel(logging.INFO)
    _add_file_handler(root, "error.log", level=logging.ERROR)
    root.propagate = False  # never bubble up to Python's own root logger
    return root


def get_app_logger() -> logging.Logger:
    """General CLI-driven events: job created/deleted, config changed,
    manual DLQ retry/delete."""
    get_root_logger()
    logger = logging.getLogger(f"{_ROOT_NAME}.app")
    logger.setLevel(logging.INFO)
    _add_file_handler(logger, "queuectl.log", level=logging.INFO)
    return logger


def get_worker_logger() -> logging.Logger:
    """Worker process lifecycle events."""
    get_root_logger()
    logger = logging.getLogger(f"{_ROOT_NAME}.worker")
    logger.setLevel(logging.INFO)
    _add_file_handler(logger, "worker.log", level=logging.INFO)
    return logger
