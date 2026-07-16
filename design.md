# design.md

Short companion to the README's Architecture Overview section — this
covers the two design decisions that most shaped the implementation.

## Why SQLite + `BEGIN IMMEDIATE` for job claiming

The hardest requirement in this assignment isn't retries or the DLQ (both
are just state-machine transitions) — it's **preventing two workers from
ever executing the same job**. That's a mutual-exclusion problem, and
SQLite already solves it: `BEGIN IMMEDIATE` acquires the database's write
lock before any statement inside the transaction runs, so if two worker
processes race to claim a job, one transaction blocks until the other
commits, and by the time it runs its own `SELECT ... WHERE state='pending'`
the job is no longer pending. One `SELECT` + `UPDATE` pair, in one
transaction, is the entire locking mechanism — no separate lock table, no
file locks, no distributed lock service.

```
worker A                          worker B
--------                          --------
BEGIN IMMEDIATE  (gets lock)
SELECT job X (pending)
UPDATE job X -> processing
COMMIT (releases lock)
                                   BEGIN IMMEDIATE (was blocked, now gets lock)
                                   SELECT ... (job X no longer pending, skips it)
                                   UPDATE job Y instead
                                   COMMIT
```

This is verified directly in `tests/test_queue_ops.py::test_concurrent_claims_never_double_claim`
(8 threads racing to claim 20 jobs — no duplicates, no lost jobs) and
end-to-end in `scripts/validate_e2e.py` (4 real worker *processes* racing
over a 20-job batch).

## Why worker control is cooperative (DB flag) instead of OS signals

`worker stop` needs to reach worker processes that were started by a
*previous, already-exited* `worker start` invocation — there's no parent
process holding onto their handles. Two options: track PIDs and send OS
signals, or have workers poll a shared flag. Signals were rejected because:

1. Windows doesn't support `SIGTERM` the way POSIX does, and this project
   was built/tested on Windows — a signal-based design would either be
   POSIX-only or need a parallel Windows-specific mechanism.
2. A flag row per worker in the `workers` table (the same table already
   needed for `status` and heartbeats) is one column (`stop_requested`)
   and needs no OS-specific code at all.

Each worker checks its own row's `stop_requested` once per loop iteration,
strictly *between* jobs — so "finish the current job before exiting" falls
out naturally from where the check is placed, rather than needing any
explicit "are we mid-job?" bookkeeping.
