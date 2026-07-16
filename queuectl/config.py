from sqlalchemy.orm import Session

from . import constants
from .exceptions import InvalidConfiguration
from .models import Config

DEFAULTS = {
    "max_retries": str(constants.DEFAULT_RETRIES),
    "backoff_base": str(constants.DEFAULT_BACKOFF_BASE),
    "poll_interval": str(constants.DEFAULT_POLL_INTERVAL),
    "heartbeat_interval": str(constants.DEFAULT_HEARTBEAT_INTERVAL),
    "timeout": str(constants.DEFAULT_TIMEOUT),
}


def load_defaults(session: Session) -> None:
    """Seed the config table with any default keys that don't already have
    a row, so the table reflects every known setting right after init
    instead of relying purely on in-code fallbacks. Idempotent -- safe to
    call on every session, which is how database.get_session() uses it."""
    existing = {row.key for row in session.query(Config).all()}
    missing = {key: value for key, value in DEFAULTS.items() if key not in existing}
    if missing:
        session.add_all(Config(key=key, value=value) for key, value in missing.items())
        session.commit()


def get_all(session: Session) -> dict:
    merged = dict(DEFAULTS)
    merged.update({row.key: row.value for row in session.query(Config).all()})
    return merged


def get_config(session: Session, key: str) -> str:
    row = session.get(Config, key)
    if row is not None:
        return row.value
    if key in DEFAULTS:
        return DEFAULTS[key]
    raise InvalidConfiguration(f"Unknown config key: {key}")


def set_config(session: Session, key: str, value: str) -> None:
    row = session.get(Config, key)
    if row is None:
        session.add(Config(key=key, value=value))
    else:
        row.value = value
    session.commit()


def reset_config(session: Session, key: str = None) -> None:
    """Reset one key (or every key, if key is None) back to its default
    by deleting the override row -- get_config then falls back to
    DEFAULTS again."""
    if key is None:
        session.query(Config).delete()
    else:
        if key not in DEFAULTS:
            raise InvalidConfiguration(f"Unknown config key: {key}")
        row = session.get(Config, key)
        if row is not None:
            session.delete(row)
    session.commit()


def get_int(session: Session, key: str) -> int:
    return int(get_config(session, key))


def get_float(session: Session, key: str) -> float:
    return float(get_config(session, key))
