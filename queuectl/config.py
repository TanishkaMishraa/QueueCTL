import sqlite3

DEFAULTS = {
    "max_retries": "3",
    "backoff_base": "2",
    "poll_interval": "1",
    "heartbeat_interval": "2",
}


def get_all(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT key, value FROM config").fetchall()
    merged = dict(DEFAULTS)
    merged.update({row["key"]: row["value"] for row in rows})
    return merged


def get(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    if row is not None:
        return row["value"]
    if key in DEFAULTS:
        return DEFAULTS[key]
    raise KeyError(f"Unknown config key: {key}")


def set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_int(conn: sqlite3.Connection, key: str) -> int:
    return int(get(conn, key))


def get_float(conn: sqlite3.Connection, key: str) -> float:
    return float(get(conn, key))
