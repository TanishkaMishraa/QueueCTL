from sqlalchemy.orm import Session

from .models import Config

DEFAULTS = {
    "max_retries": "3",
    "backoff_base": "2",
    "poll_interval": "1",
    "heartbeat_interval": "2",
}


def get_all(session: Session) -> dict:
    merged = dict(DEFAULTS)
    merged.update({row.key: row.value for row in session.query(Config).all()})
    return merged


def get(session: Session, key: str) -> str:
    row = session.get(Config, key)
    if row is not None:
        return row.value
    if key in DEFAULTS:
        return DEFAULTS[key]
    raise KeyError(f"Unknown config key: {key}")


def set(session: Session, key: str, value: str) -> None:
    row = session.get(Config, key)
    if row is None:
        session.add(Config(key=key, value=value))
    else:
        row.value = value
    session.commit()


def get_int(session: Session, key: str) -> int:
    return int(get(session, key))


def get_float(session: Session, key: str) -> float:
    return float(get(session, key))
