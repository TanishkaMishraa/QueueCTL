"""Pure validation functions for job input. No database access here --
duplicate-id checking is a repository concern (queue_ops.job_exists) since
it requires a query; everything else about a job's shape is checked here.
"""
from typing import Optional

from .exceptions import InvalidJobDataError
from .utils import parse_iso


def validate_command(command) -> str:
    if command is None or not str(command).strip():
        raise InvalidJobDataError("Job requires a non-empty 'command' field")
    return str(command)


def validate_max_retries(value) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise InvalidJobDataError("max_retries must be an integer")
    if value < 0:
        raise InvalidJobDataError("max_retries cannot be negative")
    return value


def validate_priority(value) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise InvalidJobDataError("priority must be an integer")
    if value < 0:
        raise InvalidJobDataError("priority cannot be negative")
    return value


def validate_timeout_seconds(value) -> Optional[int]:
    if value is None:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise InvalidJobDataError("timeout_seconds must be an integer")
    if value <= 0:
        raise InvalidJobDataError("timeout_seconds must be a positive number of seconds")
    return value


def validate_run_at(value):
    if value is None or value == "":
        return None
    try:
        return parse_iso(value)
    except (TypeError, ValueError) as exc:
        raise InvalidJobDataError(f"run_at must be a valid ISO timestamp: {exc}")
