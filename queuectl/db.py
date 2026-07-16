import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    run_at TEXT,
    next_attempt_at TEXT,
    timeout_seconds INTEGER,
    worker_id TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);

CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    pid INTEGER,
    status TEXT NOT NULL,
    stop_requested INTEGER NOT NULL DEFAULT 0,
    current_job_id TEXT,
    started_at TEXT,
    last_heartbeat TEXT
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_logs (
    job_id TEXT,
    attempt INTEGER,
    stdout TEXT,
    stderr TEXT,
    exit_code INTEGER,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_job_logs_job_id ON job_logs(job_id);
"""


def get_db_path() -> Path:
    override = os.environ.get("QUEUECTL_DB")
    if override:
        path = Path(override)
    else:
        path = Path.cwd() / "queuectl_data" / "queuectl.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def connect(db_path: Path = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
