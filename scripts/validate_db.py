"""Integration check for the repository layer: Create -> Store -> Read ->
Update -> Delete against a real SQLite file (no mocking), plus the config
repository's get/set/reset. Complements scripts/validate_e2e.py, which
drives the CLI and real worker processes instead of calling queue_ops.py
directly.

Run with:  python scripts/validate_db.py
"""

import shutil
import sys
import tempfile
from pathlib import Path

from queuectl import config, database, queue_ops
from queuectl.exceptions import DuplicateJobError, JobNotFoundError

results = []


def check(description: str, condition: bool) -> None:
    results.append((condition, description))
    print(("PASS" if condition else "FAIL") + f": {description}")


def main():
    tmp_dir = Path(tempfile.mkdtemp(prefix="queuectl_validate_db_"))
    db_path = tmp_dir / "validate.db"

    try:
        session = database.get_session(db_path)

        # Create
        job = queue_ops.create_job(session, {"id": "job1", "command": "echo hi", "priority": 2})
        check("Job created with expected defaults", job.state == "pending" and job.attempts == 0)

        try:
            queue_ops.create_job(session, {"id": "job1", "command": "echo again"})
            check("Duplicate id rejected", False)
        except DuplicateJobError:
            check("Duplicate id rejected", True)

        # Read
        fetched = queue_ops.get_job(session, "job1")
        check("Read returns the created job", fetched is not None and fetched.command == "echo hi")
        check("job_exists is True for a real id", queue_ops.job_exists(session, "job1"))
        check("job_exists is False for a missing id", not queue_ops.job_exists(session, "does-not-exist"))

        # Update
        updated = queue_ops.update_job(session, "job1", command="echo updated", priority=9)
        check("Update persists new field values", updated.command == "echo updated" and updated.priority == 9)

        # Restart the "process": close this session, open a brand new one
        # against the same file, and confirm the update survived.
        session.close()
        session = database.get_session(db_path)
        after_restart = queue_ops.get_job(session, "job1")
        check("Updated job persists across a restart", after_restart.command == "echo updated")

        # Config repository
        config.set_config(session, "max_retries", "7")
        check("Config override persists", config.get_int(session, "max_retries") == 7)
        config.reset_config(session, "max_retries")
        check("Config reset restores default", config.get_int(session, "max_retries") == 3)

        # Delete
        queue_ops.delete_job(session, "job1")
        check("Job no longer present after delete", queue_ops.get_job(session, "job1") is None)
        try:
            queue_ops.delete_job(session, "job1")
            check("Deleting a missing job raises JobNotFoundError", False)
        except JobNotFoundError:
            check("Deleting a missing job raises JobNotFoundError", True)

        session.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    failed = [r for r in results if not r[0]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
