import json

from click.testing import CliRunner

from queuectl.cli import main


def _invoke(runner, db_path, *args):
    return runner.invoke(main, list(args), env={"QUEUECTL_DB": str(db_path)})


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
