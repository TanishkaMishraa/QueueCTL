"""Live demo of the worker engine -- enqueues a small, varied batch of
jobs, starts workers, and prints `status` every couple of seconds so you
can watch jobs move pending -> processing -> completed/failed -> dead in
real time. Intended for recording the assignment's required CLI demo
video, not as a pass/fail check (see scripts/validate_e2e.py for that).

Run with:  python scripts/worker_demo.py
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
DEMO_DIR = REPO_ROOT / "demo_data"


def run_cli(*args, env):
    # flush=True below matters here: without it, this script's own
    # narration prints can get buffered and land after the CLI
    # subprocess's output (or vice versa) when stdout isn't a live
    # terminal -- e.g. redirected to a file while recording the demo.
    sys.stdout.flush()
    proc = subprocess.run([PYTHON, "-m", "queuectl.cli", *args], cwd=REPO_ROOT, env=env, text=True)
    return proc.returncode


def main():
    if DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR)
    DEMO_DIR.mkdir(parents=True)
    db_path = DEMO_DIR / "demo.db"

    env = dict(os.environ)
    env["QUEUECTL_DB"] = str(db_path)
    env["QUEUECTL_LOG_DIR"] = str(DEMO_DIR / "logs")

    print("=== queuectl worker demo ===", flush=True)
    print(f"Database: {db_path}", flush=True)
    print(f"Worker log: {DEMO_DIR / 'logs' / 'worker.log'}\n", flush=True)

    print("--- Configuring a fast backoff for a snappier demo ---", flush=True)
    run_cli("config", "set", "backoff-base", "1", env=env)

    print("\n--- Enqueuing jobs ---", flush=True)
    ok_cmd = f'{PYTHON} -c "print(\'hello from queuectl\')"'
    fail_cmd = f'{PYTHON} -c "import sys; sys.exit(1)"'
    run_cli("enqueue", ok_cmd, "--id", "demo-ok-1", "--priority", "high", env=env)
    run_cli("enqueue", ok_cmd, "--id", "demo-ok-2", env=env)
    run_cli("enqueue", fail_cmd, "--id", "demo-fail-1", "--max-retries", "2", env=env)
    run_cli("enqueue", "this_command_does_not_exist_xyz", "--id", "demo-invalid-1", "--max-retries", "1", env=env)

    print("\n--- Starting 2 workers ---", flush=True)
    run_cli("worker", "start", "--count", "2", env=env)

    print("\n--- Watching status for 10 seconds ---", flush=True)
    for i in range(5):
        time.sleep(2)
        print(f"\n[t+{(i + 1) * 2}s]", flush=True)
        run_cli("status", env=env)

    print("\n--- Dead Letter Queue ---", flush=True)
    run_cli("dlq", "list", env=env)

    print("\n--- Stopping workers ---", flush=True)
    run_cli("worker", "stop", env=env)

    print(f"\nDone. Inspect {DEMO_DIR / 'logs' / 'worker.log'} for the worker activity log,", flush=True)
    print(f"or {db_path} directly (DB Browser for SQLite / VS Code SQLite viewer).", flush=True)


if __name__ == "__main__":
    main()
