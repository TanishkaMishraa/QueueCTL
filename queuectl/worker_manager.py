import os
import subprocess
import sys
import time
from typing import List

from . import database
from .models import Worker
from .utils import new_id


def _spawn_detached(worker_id: str) -> None:
    args = [sys.executable, "-m", "queuectl.worker", worker_id]
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )


def start_workers(count: int, foreground: bool = False) -> List[str]:
    if count < 1:
        raise ValueError("--count must be at least 1")
    worker_ids = [new_id() for _ in range(count)]

    if foreground:
        if count != 1:
            raise ValueError("--foreground can only be combined with --count 1")
        from . import worker as worker_module

        worker_module.run(worker_ids[0])
        return worker_ids

    for worker_id in worker_ids:
        _spawn_detached(worker_id)
    return worker_ids


def stop_workers(timeout: float = 10.0) -> dict:
    session = database.get_session()
    try:
        running = session.query(Worker).filter(Worker.status == "running").all()
        worker_ids = [w.worker_id for w in running]
        if not worker_ids:
            return {"requested": 0, "stopped": 0, "worker_ids": []}

        for w in running:
            w.stop_requested = True
        session.commit()

        deadline = time.monotonic() + timeout
        stopped: set = set()
        while time.monotonic() < deadline and len(stopped) < len(worker_ids):
            rows = session.query(Worker).filter(Worker.worker_id.in_(worker_ids)).all()
            stopped = {w.worker_id for w in rows if w.status == "stopped"}
            # Every transaction on this engine opens with BEGIN IMMEDIATE
            # (see database.py), including this read-only check. Committing
            # immediately releases that write lock between polls -- without
            # it, this loop would hold the lock for its entire duration and
            # starve the very worker processes it's waiting to see commit.
            session.commit()
            if len(stopped) < len(worker_ids):
                time.sleep(0.2)

        return {"requested": len(worker_ids), "stopped": len(stopped), "worker_ids": worker_ids}
    finally:
        session.close()
