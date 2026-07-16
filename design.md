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

## Making `BEGIN IMMEDIATE` work under SQLAlchemy

The persistence layer was later ported from raw `sqlite3` to a SQLAlchemy
ORM (`database.py` + `models.py`). The locking guarantee above still had to
hold — but SQLAlchemy's default pysqlite integration actively gets in the
way of it: pysqlite normally opens its own implicit transaction on the
first DML statement and disables SQLite's native `BEGIN`, so there's no
way to ask for `BEGIN IMMEDIATE` specifically. `database.py` fixes this
with two engine event hooks (this is SQLAlchemy's own documented recipe
for serializable-style SQLite transactions, not a workaround invented
here):

```python
@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    dbapi_connection.isolation_level = None  # hand transaction control back to us

@event.listens_for(engine, "begin")
def _do_begin_immediate(conn):
    conn.exec_driver_sql("BEGIN IMMEDIATE")
```

This applies to *every* transaction on the engine, not just `claim_job`'s —
there's no cheap way to tag "this session will only read" up front. Two
real bugs showed up from that during the port, both fixed and now guarded
by tests/assertions:

1. **`DetachedInstanceError` in `cli.py`.** Several commands closed their
   session in a `finally` block and *then* read attributes off the
   returned `Job`/`Worker` objects in the `click.echo(...)` call that
   followed. SQLAlchemy sessions expire object attributes on commit by
   default, so touching them after the session is closed fails outright
   instead of silently returning stale data. Fix: build every output
   string *inside* the `try`, while the session is still open, and only
   call `click.echo` after — see `enqueue`, `status`, `list`, `dlq list`,
   `dlq retry` in `cli.py`.
2. **`worker stop` self-blocking on its own lock.** `worker_manager.stop_workers`
   polls the `workers` table in a loop until every targeted worker reports
   `status='stopped'`. The first version read via `session.query(...)`
   each iteration but never committed inside the loop — so the very first
   read's `BEGIN IMMEDIATE` write lock stayed held for the *entire*
   polling window (sleeps included), which blocked the worker processes
   from ever committing their own "I've stopped" update. The CLI would
   time out reporting 0/N confirmed, and the workers would only actually
   finish the moment the CLI gave up and closed its session, releasing
   the lock. Fix: `session.commit()` after each read inside the loop, so
   the lock is released between polls. `scripts/validate_e2e.py` now
   asserts the exact "N confirmed stopped" text so a regression here fails
   the suite instead of merely being slow.

The general lesson: with a single global `BEGIN IMMEDIATE` hook, any
session that loops and reads without committing is implicitly holding a
write lock the whole time. Every polling loop in this codebase now commits
(or closes its session) once per iteration.
