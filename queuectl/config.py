import json

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
    "default_priority": str(constants.DEFAULT_PRIORITY),
    "max_workers": str(constants.DEFAULT_MAX_WORKERS),
}


def _validate_int(name: str, value, minimum=None, exclusive_minimum=None) -> str:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise InvalidConfiguration(f"{name} must be an integer")
    if minimum is not None and parsed < minimum:
        raise InvalidConfiguration(f"{name} must be >= {minimum}")
    if exclusive_minimum is not None and parsed <= exclusive_minimum:
        raise InvalidConfiguration(f"{name} must be > {exclusive_minimum}")
    return str(parsed)


def _validate_float(name: str, value, minimum=None, exclusive_minimum=None) -> str:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise InvalidConfiguration(f"{name} must be a number")
    if minimum is not None and parsed < minimum:
        raise InvalidConfiguration(f"{name} must be >= {minimum}")
    if exclusive_minimum is not None and parsed <= exclusive_minimum:
        raise InvalidConfiguration(f"{name} must be > {exclusive_minimum}")
    return str(parsed)


# One validator per known key, applied by set_config before anything is
# written. Keeps "queuectl config set backoff-base 0.5" (nonsensical --
# backoff would shrink or never grow) or "max-retries -1" from ever
# reaching the database.
_VALIDATORS = {
    "max_retries": lambda v: _validate_int("max_retries", v, minimum=0),
    "backoff_base": lambda v: _validate_float("backoff_base", v, exclusive_minimum=1),
    "poll_interval": lambda v: _validate_float("poll_interval", v, exclusive_minimum=0),
    "heartbeat_interval": lambda v: _validate_float("heartbeat_interval", v, exclusive_minimum=0),
    "timeout": lambda v: _validate_float("timeout", v, exclusive_minimum=0),
    "max_workers": lambda v: _validate_int("max_workers", v, exclusive_minimum=0),
    "default_priority": lambda v: _validate_int("default_priority", v, minimum=0),
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


def exists(session: Session, key: str) -> bool:
    return key in DEFAULTS


def get_config(session: Session, key: str) -> str:
    row = session.get(Config, key)
    if row is not None:
        return row.value
    if key in DEFAULTS:
        return DEFAULTS[key]
    raise InvalidConfiguration(f"Unknown config key: {key}")


def set_config(session: Session, key: str, value: str) -> None:
    if key not in DEFAULTS:
        raise InvalidConfiguration(f"Unknown config key: {key}")
    validator = _VALIDATORS.get(key)
    normalized = validator(value) if validator else str(value)

    row = session.get(Config, key)
    if row is None:
        session.add(Config(key=key, value=normalized))
    else:
        row.value = normalized
    session.commit()


def delete(session: Session, key: str) -> None:
    """Remove a key's override, falling back to its default -- same effect
    as reset_config for a single key, exposed under this name too since
    `queuectl config delete <key>` reads more naturally for removing an
    override than "reset" does."""
    reset_config(session, key)


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


def _coerce_for_export(value: str):
    """Numbers round-trip as JSON numbers (not quoted strings) so an
    exported file reads naturally and diffs cleanly; anything that isn't
    numeric is left as a plain string."""
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def export_config(session: Session, path) -> dict:
    values = get_all(session)
    exportable = {key: _coerce_for_export(value) for key, value in values.items()}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(exportable, f, indent=2, sort_keys=True)
        f.write("\n")
    return exportable


def import_config(session: Session, path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as exc:
            raise InvalidConfiguration(f"Invalid JSON in {path}: {exc}")
    if not isinstance(data, dict):
        raise InvalidConfiguration("Config import file must contain a JSON object")
    for key, value in data.items():
        set_config(session, key, value)
    return get_all(session)
