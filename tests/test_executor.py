import sys

from queuectl.executor import run_command

PYTHON = sys.executable


def test_run_command_success():
    result = run_command(f'{PYTHON} -c "print(1)"')
    assert result.exit_code == 0
    assert result.stdout.strip() == "1"
    assert not result.timed_out
    assert result.duration_seconds >= 0


def test_run_command_captures_stderr_on_failure():
    result = run_command(f"{PYTHON} -c \"import sys; sys.stderr.write('boom'); sys.exit(1)\"")
    assert result.exit_code == 1
    assert "boom" in result.stderr
    assert not result.timed_out


def test_run_command_invalid_command_is_nonzero_not_an_exception():
    # A command that doesn't exist must surface as a failed ExecutionResult,
    # never as a raised Python exception -- that's what lets queue_ops
    # treat "command not found" as an ordinary, retryable failure.
    result = run_command("this_command_does_not_exist_xyz")
    assert result.exit_code != 0


def test_run_command_timeout_actually_kills_the_grandchild_process():
    # Regression test: with shell=True, a naive subprocess.run(timeout=...)
    # only kills the cmd.exe/sh -c wrapper, not the real command it
    # launched as a grandchild -- which then keeps running in the
    # background, and on Windows the call doesn't even return until that
    # orphan exits on its own. A 10s sleep with a 1s timeout must return
    # close to 1s, never anywhere near 10s.
    result = run_command(f'{PYTHON} -c "import time; time.sleep(10)"', timeout_seconds=1)
    assert result.timed_out is True
    assert result.exit_code != 0
    assert result.duration_seconds < 5
