import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional


def utcnow() -> datetime:
    # Naive UTC datetime (matches the plain datetime columns everywhere
    # else in the schema) without datetime.utcnow()'s deprecation warning.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def after_seconds(seconds: float) -> datetime:
    return utcnow() + timedelta(seconds=seconds)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def parse_iso(value) -> Optional[datetime]:
    """Parse a user-supplied ISO timestamp (e.g. a job's run_at) into a
    naive UTC datetime, matching the naive datetimes SQLAlchemy stores for
    the other DateTime columns."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt
