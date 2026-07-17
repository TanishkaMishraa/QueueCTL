"""Live demo of the worker engine -- enqueues a small, varied batch of
jobs, starts workers, and prints `status` every couple of seconds so you
can watch jobs move pending -> processing -> completed/failed -> dead in
real time. Intended for recording the assignment's required CLI demo
video, not as a pass/fail check (see scripts/validate_e2e.py for that).

Narrated in 10 numbered beats matching the assignment's suggested demo
outline: enqueue, list, start workers, successful execution, failed
execution, retry, DLQ, status, config, graceful shutdown.

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


def beat(n, title):
    print(f"\n=== [{n}/10] {title} ===", flush=True)


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
    print(f"Worker log: {DEMO_DIR / 'logs' / 'worker.log'}", flush=True)

    # A fast backoff keeps the retry beat (6) from taking real minutes.
    run_cli("config", "set", "backoff-base", "1.1", env=env)

    beat(1, "Enqueue jobs")
    ok_cmd = f"{PYTHON} -c \"print('hello from queuectl')\""
    fail_cmd = f'{PYTHON} -c "import sys; sys.exit(1)"'
    run_cli("enqueue", ok_cmd, "--id", "demo-ok-1", "--priority", "high", env=env)
    run_cli("enqueue", ok_cmd, "--id", "demo-ok-2", env=env)
    run_cli("enqueue", fail_cmd, "--id", "demo-fail-1", "--max-retries", "2", env=env)
    run_cli(
        "enqueue", "this_command_does_not_exist_xyz", "--id", "demo-invalid-1", "--max-retries", "1", env=env
    )

    beat(2, "List jobs")
    run_cli("list", env=env)

    beat(3, "Start workers")
    run_cli("worker", "start", "--count", "2", env=env)

    beat(4, "Successful execution (watch demo-ok-1/2 reach 'completed')")
    for i in range(3):
        time.sleep(2)
        print(f"\n[t+{(i + 1) * 2}s]", flush=True)
        run_cli("list", "--state", "completed", env=env)

    beat(5, "Failed execution (demo-fail-1, demo-invalid-1)")
    # With this demo's fast backoff, a job can race through 'failed' into
    # 'dead' before this check runs -- show whichever of the two it's
    # currently in rather than assuming 'failed' is still current.
    run_cli("list", "--state", "failed", env=env)
    run_cli("list", "--state", "dead", env=env)

    beat(6, "Retry in action (attempts/backoff on the failing job)")
    time.sleep(2)
    run_cli("job", "show", "demo-fail-1", env=env)

    print("\n(giving retries a moment to exhaust and land in the DLQ...)", flush=True)
    time.sleep(4)

    beat(7, "Dead Letter Queue")
    run_cli("dlq", "list", env=env)

    beat(8, "Status")
    run_cli("status", env=env)

    beat(9, "Configuration")
    run_cli("config", "list", env=env)

    beat(10, "Graceful shutdown")
    run_cli("worker", "stop", env=env)

    print(f"\nDone. Inspect {DEMO_DIR / 'logs' / 'worker.log'} for the worker activity log,", flush=True)
    print(f"or {db_path} directly (DB Browser for SQLite / VS Code SQLite viewer).", flush=True)


if __name__ == "__main__":
    main()
