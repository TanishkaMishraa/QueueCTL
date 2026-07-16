import json
from datetime import timedelta

from click.testing import CliRunner

from queuectl import database, queue_ops
from queuectl.cli import main
from queuectl.executor import ExecutionResult
from queuectl.models import Worker
from queuectl.utils import utcnow


def _invoke(runner, db_path, *args):
    return runner.invoke(main, list(args), env={"QUEUECTL_DB": str(db_path)})


def _make_stale_worker(db_path, worker_id="stale1"):
    """Insert a worker row whose heartbeat is far in the past, simulating
    a crashed worker that never got to run its own shutdown code (which
    is the only place a `workers` row is normally marked 'stopped')."""
    session = database.get_session(db_path)
    session.add(
        Worker(
            worker_id=worker_id,
            pid=999999,
            status="running",
            stop_requested=False,
            current_job_id=None,
            started_at=utcnow() - timedelta(minutes=5),
            last_heartbeat=utcnow() - timedelta(minutes=5),
        )
    )
    session.commit()
    session.close()


def _make_dead_job_via_repo(db_path, job_id="deadjob"):
    """Fail a job past its retry limit using the repository layer
    directly, so DLQ-focused CLI tests don't need a real worker process
    just to get a job into state='dead'."""
    session = database.get_session(db_path)
    queue_ops.create_job(session, {"id": job_id, "command": "exit 1", "max_retries": 1})
    job = queue_ops.claim_job(session, "w1")
    result = ExecutionResult(exit_code=1, stdout="", stderr="boom")
    queue_ops.fail_job(session, job, result, started_at=utcnow(), backoff_base=2)
    session.close()


def test_enqueue_and_list(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "cli_test.db"

    result = _invoke(runner, db_path, "enqueue", json.dumps({"id": "job1", "command": "echo hi"}))
    assert result.exit_code == 0
    assert "Job Created" in result.output
    assert "job1" in result.output

    result = _invoke(runner, db_path, "list")
    assert result.exit_code == 0
    assert "job1" in result.output
    assert "pending" in result.output


def test_enqueue_accepts_plain_command(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "cli_test.db"

    result = _invoke(runner, db_path, "enqueue", "echo Hello World", "--priority", "high", "--max-retries", "5")
    assert result.exit_code == 0
    assert "Job Created" in result.output

    result = _invoke(runner, db_path, "list")
    assert result.exit_code == 0
    assert "Hello World" in result.output


def test_enqueue_rejects_invalid_json(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "cli_test.db"
    result = _invoke(runner, db_path, "enqueue", '{"command": }')
    assert result.exit_code != 0
    assert "Invalid JSON" in result.output


def test_enqueue_rejects_invalid_priority(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "cli_test.db"
    result = _invoke(runner, db_path, "enqueue", "echo hi", "--priority", "urgent")
    assert result.exit_code != 0
    assert "Invalid --priority" in result.output


def test_enqueue_requires_command_field(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "cli_test.db"
    result = _invoke(runner, db_path, "enqueue", json.dumps({"id": "job1"}))
    assert result.exit_code != 0
    assert "command" in result.output


def test_config_set_and_get(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "cli_test.db"

    result = _invoke(runner, db_path, "config", "set", "max-retries", "5")
    assert result.exit_code == 0

    result = _invoke(runner, db_path, "config", "get", "max-retries")
    assert result.exit_code == 0
    assert "max-retries = 5" in result.output


def test_status_shows_job_counts(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "cli_test.db"
    _invoke(runner, db_path, "enqueue", json.dumps({"id": "job1", "command": "echo hi"}))

    result = _invoke(runner, db_path, "status")
    assert result.exit_code == 0
    assert "Total jobs: 1" in result.output
    assert "pending" in result.output
    assert "Workers Running: 0" in result.output


def test_status_flags_stale_worker_heartbeat(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "cli_test.db"
    _make_stale_worker(db_path, "stale1")

    result = _invoke(runner, db_path, "status")
    assert result.exit_code == 0
    # status='running' in the DB despite a 5-minute-old heartbeat --
    # crashed, but nothing ever marked it stopped. Reported running-count
    # still reflects the (stale) DB status; the STALE tag is what flags it.
    assert "Workers Running: 1" in result.output
    assert "STALE" in result.output


def test_worker_list_shows_stale_marker(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "cli_test.db"
    _make_stale_worker(db_path, "stale1")

    result = _invoke(runner, db_path, "worker", "list")
    assert result.exit_code == 0
    assert "stale1" in result.output
    assert "STALE" in result.output


def test_worker_list_empty(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "cli_test.db"
    result = _invoke(runner, db_path, "worker", "list")
    assert result.exit_code == 0
    assert "Workers Running: 0" in result.output


def test_dlq_list_empty(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "cli_test.db"
    result = _invoke(runner, db_path, "dlq", "list")
    assert result.exit_code == 0
    assert "DLQ is empty" in result.output


def test_dlq_retry_missing_job(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "cli_test.db"
    result = _invoke(runner, db_path, "dlq", "retry", "nope")
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_dlq_count(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "cli_test.db"

    result = _invoke(runner, db_path, "dlq", "count")
    assert result.exit_code == 0
    assert "Dead Jobs: 0" in result.output

    _make_dead_job_via_repo(db_path, "deadjob")
    result = _invoke(runner, db_path, "dlq", "count")
    assert result.exit_code == 0
    assert "Dead Jobs: 1" in result.output


def test_dlq_delete_with_confirmation(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "cli_test.db"
    _make_dead_job_via_repo(db_path, "deadjob")

    # Decline first -- job should still be in the DLQ afterward.
    result = runner.invoke(main, ["dlq", "delete", "deadjob"], input="n\n", env={"QUEUECTL_DB": str(db_path)})
    assert result.exit_code == 0
    assert "Aborted" in result.output
    result = _invoke(runner, db_path, "dlq", "count")
    assert "Dead Jobs: 1" in result.output

    # Confirm -- job should be gone.
    result = runner.invoke(main, ["dlq", "delete", "deadjob"], input="y\n", env={"QUEUECTL_DB": str(db_path)})
    assert result.exit_code == 0
    assert "Deleted job deadjob" in result.output
    result = _invoke(runner, db_path, "dlq", "count")
    assert "Dead Jobs: 0" in result.output


def test_dlq_delete_yes_flag_skips_confirmation(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "cli_test.db"
    _make_dead_job_via_repo(db_path, "deadjob")

    result = _invoke(runner, db_path, "dlq", "delete", "deadjob", "--yes")
    assert result.exit_code == 0
    assert "Deleted job deadjob" in result.output


def test_dlq_delete_rejects_non_dead_job(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "cli_test.db"
    _invoke(runner, db_path, "enqueue", json.dumps({"id": "job1", "command": "echo hi"}))

    result = _invoke(runner, db_path, "dlq", "delete", "job1", "--yes")
    assert result.exit_code != 0
    assert "not in the dlq" in result.output.lower()


def test_job_show(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "cli_test.db"
    _invoke(runner, db_path, "enqueue", json.dumps({"id": "job1", "command": "echo hi"}))

    result = _invoke(runner, db_path, "job", "show", "job1")
    assert result.exit_code == 0
    assert "job1" in result.output
    assert "echo hi" in result.output
    assert "pending" in result.output


def test_job_show_missing_job(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "cli_test.db"
    result = _invoke(runner, db_path, "job", "show", "nope")
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_job_delete_with_confirmation(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "cli_test.db"
    _invoke(runner, db_path, "enqueue", json.dumps({"id": "job1", "command": "echo hi"}))

    # Decline first -- job should still exist afterward.
    result = runner.invoke(main, ["job", "delete", "job1"], input="n\n", env={"QUEUECTL_DB": str(db_path)})
    assert result.exit_code == 0
    assert "Aborted" in result.output
    result = _invoke(runner, db_path, "job", "show", "job1")
    assert result.exit_code == 0

    # Confirm -- job should be gone.
    result = runner.invoke(main, ["job", "delete", "job1"], input="y\n", env={"QUEUECTL_DB": str(db_path)})
    assert result.exit_code == 0
    assert "Deleted job job1" in result.output
    result = _invoke(runner, db_path, "job", "show", "job1")
    assert result.exit_code != 0


def test_job_delete_yes_flag_skips_confirmation(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "cli_test.db"
    _invoke(runner, db_path, "enqueue", json.dumps({"id": "job1", "command": "echo hi"}))

    result = _invoke(runner, db_path, "job", "delete", "job1", "--yes")
    assert result.exit_code == 0
    assert "Deleted job job1" in result.output


def test_job_delete_missing_job(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "cli_test.db"
    result = _invoke(runner, db_path, "job", "delete", "nope", "--yes")
    assert result.exit_code != 0
    assert "not found" in result.output.lower()
