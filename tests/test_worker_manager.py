"""Regression test for a real bug: a detached worker process resolving a
different working directory than the CLI process that spawned it, so it
silently read/wrote a different queuectl_data/queuectl.db than every
other command (see design.md, "Bug: detached workers writing to the
wrong database"). Fixed by passing cwd= explicitly rather than relying on
implicit inheritance, which Windows doesn't reliably honor for a process
started with DETACHED_PROCESS.
"""

import os
from unittest.mock import patch

from queuectl import worker_manager


def test_spawn_detached_pins_child_cwd_to_current_directory():
    with patch("queuectl.worker_manager.subprocess.Popen") as mock_popen:
        worker_manager._spawn_detached("worker1")

    assert mock_popen.called
    _, kwargs = mock_popen.call_args
    assert kwargs.get("cwd") == os.getcwd()
