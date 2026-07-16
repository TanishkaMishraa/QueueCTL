import sqlite3
from typing import List, Optional

from . import config as config_mod
from .execution import ExecutionResult
from .models import Job, State
from .utils import iso_after, new_id, now_iso


def enqueue_job(conn: sqlite3.Connection, data: dict) -> Job:
    command = data.get("command")
    if not command or not str(command).strip():
        raise ValueError("Job requires a non-empty 'command' field")

    job_id = str(data.get("id") or new_id())
    if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone():
        raise ValueError(f"Job id already exists: {job_id}")

    max_retries = int(data.get("max_retries", config_mod.get_int(conn, "max_retries")))
    priority = int(data.get("priority", 0))
    run_at = data.get("run_at")
    timeout_seconds = data.get("timeout_seconds")
    now = now_iso()

    conn.execute(
        """
        INSERT INTO jobs (id, command, state, attempts, max_retries, priority,
                           run_at, next_attempt_at, timeout_seconds, worker_id,
                           last_error, created_at, updated_at)
        VALUES (?, ?, 'pending', 0, ?, ?, ?, NULL, ?, NULL, NULL, ?, ?)
        """,
        (job_id, str(command), max_retries, priority, run_at, timeout_seconds, now, now),
    )
    return get_job(conn, job_id)


def get_job(conn: sqlite3.Connection, job_id: str) -> Optional[Job]:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return Job.from_row(row) if row else None


def claim_job(conn: sqlite3.Connection, worker_id: str) -> Optional[Job]:
    """Atomically select and claim one eligible job.

    BEGIN IMMEDIATE takes the write lock before reading, so SQLite
    serializes concurrent callers on this transaction: only one worker can
    ever move a given job out of the eligible set, which is what prevents
    duplicate processing across worker processes.
    """
    now = now_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """
            SELECT * FROM jobs
            WHERE (run_at IS NULL OR run_at <= ?)
              AND (
                    state = ?
                    OR (state = ? AND next_attempt_at IS NOT NULL AND next_attempt_at <= ?)
                  )
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
            """,
            (now, State.PENDING, State.FAILED, now),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        job = Job.from_row(row)
        conn.execute(
            "UPDATE jobs SET state = ?, worker_id = ?, updated_at = ? WHERE id = ?",
            (State.PROCESSING, worker_id, now, job.id),
        )
        conn.execute("COMMIT")
        job.state = State.PROCESSING
        job.worker_id = worker_id
        job.updated_at = now
        return job
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _log_attempt(
    conn: sqlite3.Connection,
    job_id: str,
    attempt: int,
    result: ExecutionResult,
    started_at: str,
    finished_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO job_logs (job_id, attempt, stdout, stderr, exit_code, started_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (job_id, attempt, result.stdout, result.stderr, result.exit_code, started_at, finished_at),
    )


def complete_job(
    conn: sqlite3.Connection, job: Job, result: ExecutionResult, started_at: str
) -> None:
    now = now_iso()
    attempts = job.attempts + 1
    conn.execute(
        "UPDATE jobs SET state = ?, attempts = ?, updated_at = ?, last_error = NULL, "
        "next_attempt_at = NULL WHERE id = ?",
        (State.COMPLETED, attempts, now, job.id),
    )
    _log_attempt(conn, job.id, attempts, result, started_at, now)


def fail_job(
    conn: sqlite3.Connection,
    job: Job,
    result: ExecutionResult,
    started_at: str,
    backoff_base: float,
) -> None:
    now = now_iso()
    attempts = job.attempts + 1
    reason = result.stderr.strip() or result.stdout.strip() or f"exit code {result.exit_code}"
    error_msg = reason[:2000]

    if attempts >= job.max_retries:
        conn.execute(
            "UPDATE jobs SET state = ?, attempts = ?, updated_at = ?, last_error = ?, "
            "next_attempt_at = NULL WHERE id = ?",
            (State.DEAD, attempts, now, error_msg, job.id),
        )
    else:
        delay_seconds = backoff_base ** attempts
        next_attempt_at = iso_after(delay_seconds)
        conn.execute(
            "UPDATE jobs SET state = ?, attempts = ?, updated_at = ?, last_error = ?, "
            "next_attempt_at = ? WHERE id = ?",
            (State.FAILED, attempts, now, error_msg, next_attempt_at, job.id),
        )
    _log_attempt(conn, job.id, attempts, result, started_at, now)


def list_jobs(conn: sqlite3.Connection, state: Optional[str] = None, limit: Optional[int] = None) -> List[Job]:
    query = "SELECT * FROM jobs"
    params: list = []
    if state:
        query += " WHERE state = ?"
        params.append(state)
    query += " ORDER BY created_at DESC"
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [Job.from_row(r) for r in rows]


def dlq_list(conn: sqlite3.Connection) -> List[Job]:
    return list_jobs(conn, state=State.DEAD)


def dlq_retry(conn: sqlite3.Connection, job_id: str) -> Job:
    job = get_job(conn, job_id)
    if job is None:
        raise KeyError(f"Job not found: {job_id}")
    if job.state != State.DEAD:
        raise ValueError(f"Job {job_id} is not in the DLQ (current state: {job.state})")
    now = now_iso()
    conn.execute(
        "UPDATE jobs SET state = ?, attempts = 0, next_attempt_at = NULL, last_error = NULL, "
        "worker_id = NULL, updated_at = ? WHERE id = ?",
        (State.PENDING, now, job_id),
    )
    return get_job(conn, job_id)


def status_summary(conn: sqlite3.Connection) -> dict:
    counts = {s: 0 for s in State.ALL}
    for row in conn.execute("SELECT state, COUNT(*) AS c FROM jobs GROUP BY state").fetchall():
        counts[row["state"]] = row["c"]

    workers = [dict(w) for w in conn.execute("SELECT * FROM workers ORDER BY started_at").fetchall()]

    stats_row = conn.execute(
        "SELECT COUNT(*) AS attempts, "
        "SUM(CASE WHEN exit_code = 0 THEN 1 ELSE 0 END) AS successes "
        "FROM job_logs"
    ).fetchone()
    total_attempts = stats_row["attempts"] or 0
    successes = stats_row["successes"] or 0
    success_rate = round((successes / total_attempts) * 100, 1) if total_attempts else 0.0

    return {
        "jobs_total": sum(counts.values()),
        "by_state": counts,
        "workers": workers,
        "total_attempts_logged": total_attempts,
        "success_rate_pct": success_rate,
    }
