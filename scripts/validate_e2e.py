"""End-to-end validation of the assignment's 5 required test scenarios,
plus 2 supplementary ones covering graceful shutdown and crash resilience
under concurrent workers (Phase 7's milestone 7.11: "shutdown" and
"crash" test cases).

Run with:  python scripts/validate_e2e.py

Drives the real `queuectl` CLI as subprocesses against a fresh, isolated
SQLite database (no pytest, no mocking of subprocess execution) so it
exercises the exact same code path a user would.
"""
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def run_cli(*args, env, check=True):
    proc = subprocess.run(
        [PYTHON, "-m", "queuectl.cli", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"CLI command failed: {args}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc


def query_db(db_path, sql, params=()):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def wait_until(predicate, timeout=20.0, interval=0.3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def make_env(db_path):
    env = dict(os.environ)
    env["QUEUECTL_DB"] = str(db_path)
    return env


def force_kill(pid):
    """Terminate a process the way a real crash would -- uncatchable, no
    chance for the worker's own shutdown code to run. On POSIX that's
    SIGKILL; os.kill(pid, SIGTERM) on Windows is handled specially by
    CPython as a direct TerminateProcess call, which Python signal
    handlers never see either, so it has the same effect there."""
    if os.name == "nt":
        os.kill(pid, signal.SIGTERM)
    else:
        os.kill(pid, signal.SIGKILL)


def scenario_1_basic_completion(tmp_dir):
    db_path = tmp_dir / "s1.db"
    env = make_env(db_path)
    cmd = f'{PYTHON} -c "print(1)"'
    run_cli("enqueue", json.dumps({"id": "job1", "command": cmd}), env=env)
    run_cli("worker", "start", "--count", "1", env=env)
    try:
        ok = wait_until(
            lambda: query_db(db_path, "SELECT state FROM jobs WHERE id='job1'")[0]["state"] == "completed"
        )
        assert ok, "job1 did not reach 'completed' state"
    finally:
        stop_out = run_cli("worker", "stop", env=env).stdout
        assert "1 confirmed stopped" in stop_out, f"worker did not confirm graceful stop in time: {stop_out!r}"
    return "Basic job completes successfully"


def scenario_2_retry_backoff_dlq(tmp_dir):
    db_path = tmp_dir / "s2.db"
    env = make_env(db_path)
    # Small backoff base so the scenario finishes quickly; exponential math
    # itself is covered precisely by tests/test_queue_ops.py.
    run_cli("config", "set", "max-retries", "2", env=env)
    run_cli("config", "set", "backoff-base", "1", env=env)
    cmd = f'{PYTHON} -c "import sys; sys.exit(1)"'
    run_cli("enqueue", json.dumps({"id": "job2", "command": cmd, "max_retries": 2}), env=env)
    run_cli("worker", "start", "--count", "1", env=env)
    try:
        ok = wait_until(
            lambda: query_db(db_path, "SELECT state FROM jobs WHERE id='job2'")[0]["state"] == "dead",
            timeout=30,
        )
        assert ok, "job2 did not reach the DLQ"
        row = query_db(db_path, "SELECT attempts FROM jobs WHERE id='job2'")[0]
        assert row["attempts"] == 2, f"expected 2 attempts, got {row['attempts']}"
        dlq_out = run_cli("dlq", "list", env=env).stdout
        assert "job2" in dlq_out
    finally:
        run_cli("worker", "stop", env=env)
    return "Failed job retries with backoff and moves to DLQ"


def scenario_3_parallel_workers_no_duplicates(tmp_dir):
    db_path = tmp_dir / "s3.db"
    env = make_env(db_path)
    n_jobs = 20
    for i in range(n_jobs):
        cmd = f'{PYTHON} -c "pass"'
        run_cli("enqueue", json.dumps({"id": f"batch{i}", "command": cmd}), env=env)
    run_cli("worker", "start", "--count", "4", env=env)
    try:
        ok = wait_until(
            lambda: query_db(db_path, "SELECT COUNT(*) AS c FROM jobs WHERE state='completed'")[0]["c"] == n_jobs,
            timeout=30,
        )
        assert ok, "not all jobs completed"
        dupe_rows = query_db(
            db_path,
            "SELECT job_id, COUNT(*) AS c FROM job_logs GROUP BY job_id HAVING c > 1",
        )
        assert not dupe_rows, f"jobs executed more than once: {[r['job_id'] for r in dupe_rows]}"
    finally:
        stop_out = run_cli("worker", "stop", env=env).stdout
        assert "4 confirmed stopped" in stop_out, f"not all 4 workers confirmed graceful stop: {stop_out!r}"
    return "Multiple workers process jobs without overlap"


def scenario_4_invalid_command_fails_gracefully(tmp_dir):
    db_path = tmp_dir / "s4.db"
    env = make_env(db_path)
    run_cli("config", "set", "max-retries", "1", env=env)
    run_cli(
        "enqueue",
        json.dumps({"id": "job4", "command": "this_command_does_not_exist_xyz", "max_retries": 1}),
        env=env,
    )
    run_cli("worker", "start", "--count", "1", env=env)
    try:
        ok = wait_until(
            lambda: query_db(db_path, "SELECT state FROM jobs WHERE id='job4'")[0]["state"] == "dead",
            timeout=20,
        )
        assert ok, "invalid command job did not fail gracefully into the DLQ"
    finally:
        run_cli("worker", "stop", env=env)
    return "Invalid commands fail gracefully"


def scenario_5_persistence_across_restart(tmp_dir):
    db_path = tmp_dir / "s5.db"
    env = make_env(db_path)
    run_cli("enqueue", json.dumps({"id": "job5a", "command": "echo hi"}), env=env)
    run_cli("enqueue", json.dumps({"id": "job5b", "command": "echo hi"}), env=env)

    # "Restart": no process/connection from above is reused here at all;
    # this invokes a brand new interpreter process against the same file.
    out = run_cli("list", env=env).stdout
    assert "job5a" in out and "job5b" in out, "jobs did not survive restart"
    return "Job data survives restart"


def scenario_6_graceful_shutdown_waits_for_current_job(tmp_dir):
    db_path = tmp_dir / "s6.db"
    env = make_env(db_path)
    cmd = f'{PYTHON} -c "import time; time.sleep(3)"'
    run_cli("enqueue", json.dumps({"id": "job6", "command": cmd}), env=env)
    run_cli("worker", "start", "--count", "1", env=env)
    try:
        picked_up = wait_until(
            lambda: query_db(db_path, "SELECT state FROM jobs WHERE id='job6'")[0]["state"] == "processing",
            timeout=10,
        )
        assert picked_up, "job6 was never claimed by the worker"

        # Ask the worker to stop while job6 is still mid-sleep. A generous
        # --timeout gives stop_workers room to actually observe the ~3s
        # job finish, rather than giving up before it does.
        stop_out = run_cli("worker", "stop", "--timeout", "15", env=env).stdout
        assert "1 confirmed stopped" in stop_out, f"worker did not shut down gracefully: {stop_out!r}"

        state = query_db(db_path, "SELECT state FROM jobs WHERE id='job6'")[0]["state"]
        assert state == "completed", f"job6 should have finished before the worker stopped, got state={state!r}"
    finally:
        run_cli("worker", "stop", env=env)
    return "Graceful shutdown finishes the in-flight job before the worker exits"


def scenario_7_worker_crash_others_continue(tmp_dir):
    db_path = tmp_dir / "s7.db"
    env = make_env(db_path)
    n_jobs = 12
    # Each job takes a moment so there's an actual window to kill a
    # worker mid-batch instead of after everything's already done.
    cmd = f'{PYTHON} -c "import time; time.sleep(0.5)"'
    for i in range(n_jobs):
        run_cli("enqueue", json.dumps({"id": f"crash{i}", "command": cmd}), env=env)
    run_cli("worker", "start", "--count", "3", env=env)
    try:
        got_workers = wait_until(
            lambda: len(query_db(db_path, "SELECT pid FROM workers WHERE status='running'")) == 3,
            timeout=20,
        )
        assert got_workers, "not all 3 workers registered in time"
        victim = query_db(db_path, "SELECT worker_id, pid FROM workers WHERE status='running'")[0]
        force_kill(victim["pid"])

        ok = wait_until(
            lambda: query_db(db_path, "SELECT COUNT(*) AS c FROM jobs WHERE state='completed'")[0]["c"] == n_jobs,
            timeout=30,
        )
        assert ok, "surviving workers did not finish all jobs after one was killed"

        dupe_rows = query_db(
            db_path,
            "SELECT job_id, COUNT(*) AS c FROM job_logs GROUP BY job_id HAVING c > 1",
        )
        assert not dupe_rows, f"jobs executed more than once: {[r['job_id'] for r in dupe_rows]}"

        # A crash never runs the worker's own shutdown code (the only
        # place a `workers` row is marked 'stopped'), so the killed
        # worker's row should still read 'running' -- this is exactly
        # why `status`'s heartbeat-staleness check exists.
        victim_status = query_db(
            db_path, "SELECT status FROM workers WHERE worker_id=?", (victim["worker_id"],)
        )[0]["status"]
        assert victim_status == "running", (
            f"killed worker's row should still read 'running' (nothing marks it stopped on a "
            f"crash) -- got {victim_status!r}"
        )
    finally:
        run_cli("worker", "stop", "--timeout", "3", env=env)
    return "Killing one worker doesn't stop the others, and no job runs twice"


def main():
    tmp_dir = Path(tempfile.mkdtemp(prefix="queuectl_e2e_"))
    scenarios = [
        scenario_1_basic_completion,
        scenario_2_retry_backoff_dlq,
        scenario_3_parallel_workers_no_duplicates,
        scenario_4_invalid_command_fails_gracefully,
        scenario_5_persistence_across_restart,
        scenario_6_graceful_shutdown_waits_for_current_job,
        scenario_7_worker_crash_others_continue,
    ]
    results = []
    try:
        for scenario in scenarios:
            try:
                description = scenario(tmp_dir)
                results.append((True, description))
                print(f"PASS: {description}")
            except Exception as exc:
                results.append((False, f"{scenario.__name__}: {exc}"))
                print(f"FAIL: {scenario.__name__}: {exc}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    failed = [r for r in results if not r[0]]
    print(f"\n{len(results) - len(failed)}/{len(results)} scenarios passed.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
