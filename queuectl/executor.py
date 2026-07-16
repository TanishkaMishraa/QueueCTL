import subprocess
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class ExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float = 0.0
    timed_out: bool = False


def run_command(command: str, timeout_seconds: Optional[int] = None) -> ExecutionResult:
    """Run a job's shell command and capture its outcome.

    Uses the OS default shell (cmd.exe on Windows, /bin/sh on POSIX) so a
    missing/invalid command naturally surfaces as a non-zero exit code
    rather than a Python exception, keeping success/failure detection to a
    single exit-code check.
    """
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return ExecutionResult(
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_seconds=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return ExecutionResult(
            exit_code=-1,
            stdout=stdout,
            stderr=stderr + f"\n[queuectl] command timed out after {timeout_seconds}s",
            duration_seconds=time.monotonic() - started,
            timed_out=True,
        )
    except OSError as exc:
        return ExecutionResult(
            exit_code=127,
            stdout="",
            stderr=f"[queuectl] failed to start command: {exc}",
            duration_seconds=time.monotonic() - started,
        )
