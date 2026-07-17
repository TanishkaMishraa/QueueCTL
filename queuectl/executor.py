import os
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


def _kill_process_tree(proc: "subprocess.Popen") -> None:
    """Kill the whole process tree, not just the immediate shell child.

    With shell=True, `proc` is cmd.exe (Windows) or `sh -c` (POSIX)
    wrapping the real command as a grandchild. subprocess.run's built-in
    timeout handling only kills that direct child; the grandchild it
    launched keeps running to completion in the background -- and on
    Windows, the timed-out call doesn't even return until that orphaned
    grandchild exits on its own and releases its handle on the shared
    stdout/stderr pipes. Observed directly: a `time.sleep(5)` job with
    timeout_seconds=1 took ~5.3s to return instead of ~1s.
    """
    if os.name == "nt":
        # taskkill's /T recursively kills every process whose parent
        # process ID chains back to this one -- exactly the shell -> real
        # command relationship shell=True creates.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        import signal

        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_command(command: str, timeout_seconds: Optional[int] = None) -> ExecutionResult:
    """Run a job's shell command and capture its outcome.

    Uses the OS default shell (cmd.exe on Windows, /bin/sh on POSIX) so a
    missing/invalid command naturally surfaces as a non-zero exit code
    rather than a Python exception, keeping success/failure detection to a
    single exit-code check.
    """
    started = time.monotonic()
    popen_kwargs = {}
    if os.name != "nt":
        # Makes the shell the leader of its own process group, so on
        # timeout we can kill that whole group without touching queuectl's
        # own process group.
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **popen_kwargs,
        )
    except OSError as exc:
        return ExecutionResult(
            exit_code=127,
            stdout="",
            stderr=f"[queuectl] failed to start command: {exc}",
            duration_seconds=time.monotonic() - started,
        )

    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        return ExecutionResult(
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        stdout, stderr = proc.communicate()  # drain whatever's buffered, now that it's dead
        return ExecutionResult(
            exit_code=-1,
            stdout=stdout or "",
            stderr=(stderr or "") + f"\n[queuectl] command timed out after {timeout_seconds}s",
            duration_seconds=time.monotonic() - started,
            timed_out=True,
        )
