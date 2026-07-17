# design.md

Companion to the README's Architecture Overview section. That section
covers what each layer/module is responsible for in detail; this file is
the "why" behind the decisions and bugs that most shaped the
implementation, in the order they came up.

## System architecture

```
CLI (cli.py)
   |
   v
Repository layer (queue_ops.py, config.py, dlq.py, metrics.py)
   |  <- the only modules that call Session.query/add/delete/commit
   v
Validation (validators.py) + Models (models.py)
   |
   v
Database engine (database.py)
   |
   v
SQLite (queuectl_data/queuectl.db)
```

`dlq.py` and `metrics.py` don't talk to `Session` themselves; they call
into `queue_ops.py`, which is the only module with ORM queries in it (see
the README's Repository layer / DLQ sections for why).

## Worker architecture

```
queuectl worker start --count N
   |
   v
worker_manager.start_workers()
   |
   |  spawns N independent OS processes (subprocess.Popen, detached)
   v
worker.py: run()  x N   <-- each is its own process, own SQLAlchemy Session
   |
   |  loop: claim_job -> executor.run_command -> complete_job/fail_job
   v
Same shared SQLite database file
```

## Job lifecycle

```
        enqueue
           |
           v
       pending  <---------------------+
           |                          |
           | claim_job (BEGIN IMMEDIATE)
           v                          |
       processing                    | next_retry elapsed
           |                         |
     executor.run_command            |
           |                         |
   exit 0  |   exit != 0             |
           v         v               |
      completed   failed  ----------+
                      |
                      | attempts >= max_retries
                      v
                     dead  (== the DLQ)
```

## Sequence: a job from enqueue to completion

```
User          CLI (cli.py)      queue_ops.py         SQLite          worker.py (separate process)
 |                |                   |                 |                      |
 |  enqueue "..." |                   |                 |                      |
 |--------------->|                   |                 |                      |
 |                | create_job(data)  |                 |                      |
 |                |------------------>|                 |                      |
 |                |                   | INSERT jobs...   |                      |
 |                |                   |---------------->|                      |
 |                |                   |<-----------------|                      |
 |                |<------------------|                 |                      |
 |<---------------|  "Job Created"    |                 |                      |
 |                |                   |                 |   (already polling)  |
 |                |                   |                 |<---------------------|
 |                |                   |                 |  BEGIN IMMEDIATE;    |
 |                |                   |                 |  SELECT pending;     |
 |                |                   |                 |  UPDATE processing;  |
 |                |                   |                 |  COMMIT              |
 |                |                   |                 |--------------------->|
 |                |                   |                 |     run_command()    |
 |                |                   |                 |<---------------------|
 |                |                   |                 |  complete_job/       |
 |                |                   |                 |  fail_job (commit)   |
 |  status        |                   |                 |                      |
 |--------------->| status_summary()  |                 |                      |
 |                |------------------>| SELECT counts    |                      |
 |                |                   |---------------->|                      |
 |<---------------|<------------------|<-----------------|                      |
```

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

## Why validation, duplicate-checking, and exceptions live in three different places

It would be simpler to shove all of it into one big `if` block at the top
of `create_job`. Splitting it up on purpose:

- **`validators.py` never touches the database.** Each `validate_*`
  function checks one field in isolation and raises `InvalidJobDataError`.
  That means they're trivially unit-testable without a session fixture,
  and they can be reused by both `create_job` and `update_job` (a job's
  `priority` gets the exact same "must be >= 0" rule whether it's set at
  creation or changed later) instead of duplicating the check.
- **Duplicate-id checking stays in `queue_ops.create_job`, not
  validators.py**, because it's the one check that *does* need a database
  read (`job_exists`). Keeping "pure field shape" and "does this conflict
  with existing data" as two different kinds of check makes it obvious,
  reading either file, which category a given failure belongs to.
- **Exceptions all subclass one `QueueCTLError`** specifically so `cli.py`
  can catch one thing per command instead of an ever-growing tuple
  (`except (KeyError, ValueError, InvalidJobDataError, ...)`). Each
  command still gets a clean, specific message, because the exception's
  own `str()` carries that detail — the CLI layer just needs to know
  "this was an expected, user-facing failure" versus "something
  unexpected broke," not the exact subclass.
- **`update_job` has an explicit allow-list of editable fields**
  (`command`, `max_retries`, `priority`, `run_at`, `timeout_seconds`)
  rather than accepting arbitrary `**fields` and setting whatever
  attribute name shows up. Lifecycle fields (`state`, `attempts`,
  `next_retry`, `worker_id`, `last_error`) can only change through
  `claim_job`/`complete_job`/`fail_job`/`dlq_retry` — if `update_job` also
  let a caller set `state="dead"` directly, there would be two competing
  ways to move a job into the DLQ, and the atomic-claim guarantee
  wouldn't protect a state written through this door.

## Why a retryable failure stays visibly `failed` instead of resetting straight to `pending`

A tempting simplification for scheduling a retry: reset the job's `state`
back to `pending` immediately, with a future `next_retry`, and let the
claim query's `next_retry <= now` filter hold it back. Nothing about
correctness breaks if you do this — the job still won't be claimed early.

It was deliberately not done, though, because the assignment's own Job
Lifecycle table defines `failed` as a distinct, meaningful state
("Failed, but retryable") alongside `pending`, `processing`, `completed`,
`dead` — five states, not four. If a retry-scheduled job's `state` reads
`pending`, there is no way to tell it apart from a job that has never been
attempted at all: `queuectl list --state pending` and `queuectl status`
would both undercount "how many jobs have actually failed at least once"
and silently misreport `pending` as larger than it really is. Keeping
`state='failed'` until the backoff elapses — and having the claim query
treat `pending` and `(failed AND next_retry <= now)` as equally eligible —
gets the same "don't retry early" behavior without losing that
observability. The cost is one extra `OR` clause in `claim_job`'s `WHERE`;
the benefit is that every one of the assignment's five documented states
is actually reachable and visible through the CLI.

## Bug: detached workers writing to the wrong database (wrong working directory)

Reported symptom: run `queuectl enqueue ...`, `queuectl worker start
--count 2`, then `queuectl status`/`worker stop` — and it reports zero
running workers, even though `Get-Process python*` shows the worker
processes are genuinely alive.

Root cause: `worker_manager._spawn_detached` calls `subprocess.Popen(args,
creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS, ...)` without
passing `cwd=`. The intent was that the child inherits the parent CLI
process's current directory (Python's documented default when `cwd` is
omitted), so it resolves the same default `./queuectl_data/queuectl.db`
path. On Windows, though, a process launched with `DETACHED_PROCESS` and
no explicit working directory can end up with a different effective
current directory than the parent -- confirmed on a real machine by
finding a second, unexpected `queuectl_data/` folder created somewhere
other than the project directory. The worker process was alive and
working correctly; it was just quietly reading and writing an entirely
different SQLite file than every other `queuectl` command.

This is exactly the kind of bug that a test suite driven from one
consistent working directory (pytest, `validate_e2e.py`, or this
project's own manual smoke-testing via a single shell) will never catch,
since every command in a test run naturally shares the same cwd already
-- it only surfaces when a real user runs `worker start` and a later
command from what looks like "the same place" but resolves differently
at the OS process-creation level.

Fix: pass `cwd=os.getcwd()` explicitly to `Popen` in `_spawn_detached`,
so the worker's working directory is pinned to exactly what the CLI
process had at the moment `worker start` ran, with no dependence on
whatever Windows' default inheritance behavior happens to do for a
detached process.

## Bug: job timeouts didn't actually kill anything (shell=True + subprocess.run's timeout)

Found while writing `tests/test_executor.py` for Phase 10's coverage
pass — not by inspection, by the test's timing failing:
`run_command('python -c "time.sleep(5)"', timeout_seconds=1)` returned
after **5.3 seconds**, not ~1.

Root cause: `executor.run_command` ran the job via `subprocess.run(command,
shell=True, timeout=timeout_seconds)`. With `shell=True`, the process
`subprocess.run` actually starts is `cmd.exe /c <command>` (or `sh -c
<command>` on POSIX) — the real command runs as a *grandchild*, one level
below. When the timeout fires, `subprocess.run`'s internals kill only the
direct child (the shell), not the grandchild it launched. The grandchild
keeps running to completion in the background — and on Windows, the
call doesn't even return in ~1s as expected, because `communicate()` is
still blocked reading the shared stdout/stderr pipes, which don't get a
final EOF until every process holding a handle to them exits, including
the still-running orphaned grandchild. So a job with a 1-second timeout
that internally runs something slow would appear to hang for however long
that something actually takes — silently defeating the entire timeout
feature.

Fix: switched from `subprocess.run(..., timeout=...)` to
`subprocess.Popen` + `proc.communicate(timeout=...)`, and on
`TimeoutExpired`, kill the *whole process tree* instead of just `proc`:
`taskkill /F /T /PID <pid>` on Windows (`/T` recursively kills anything
whose parent-process chain leads back to `pid`, i.e. the shell and
whatever it launched), or `os.killpg(os.getpgid(pid), SIGKILL)` on POSIX
(the shell is started via `start_new_session=True` specifically so it
leads its own process group, letting that group be killed without
touching queuectl's own).

The general lesson, consistent with the `cwd` bug above: a few of the
real bugs in this project were only found by exercising the actual
process-level behavior end-to-end (or, here, by timing a real subprocess
call) — not by reading the code, and not by a unit test that mocks
subprocess away. `capture_output=True` in the old code even looked
correct at a glance; the bug was entirely about what a signal/timeout
does to a *process tree* it didn't fully control.

## Bug: `benchmark`'s own polling loop starved the workers it just started

Found immediately when manually smoke-testing the new `queuectl benchmark`
command (Phase 10's optional performance-testing feature): `queuectl
benchmark --jobs 30 --workers 4` enqueued 30 jobs, started 4 workers, and
then reported **0/30 completed** after the full timeout — every time,
no matter how simple the job command was.

Root cause: exactly the bug already documented and fixed once before in
`worker_manager.stop_workers` (see the "Concurrency bugs from the
SQLAlchemy port" section above) — recreated fresh in new code. The
benchmark command's wait loop looked like:

```python
completed = queue_ops.count_jobs(session, state=State.COMPLETED)
while time.monotonic() < deadline and completed < job_count:
    time.sleep(0.2)
    completed = queue_ops.count_jobs(session, state=State.COMPLETED)
```

`count_jobs` never commits. Since every transaction on this engine opens
with `BEGIN IMMEDIATE` (database.py's `begin` event hook), the *first*
call in this loop takes SQLite's write lock and never releases it —
every subsequent iteration reuses that same still-open transaction, and
the loop just keeps re-reading a snapshot from the moment it started.
Meanwhile, the 4 freshly-spawned workers all sit blocked inside their own
`claim_job` call, waiting up to `busy_timeout` (30s) for a write lock this
process is holding indefinitely. Nothing ever gets claimed, so nothing
ever completes, so the count never changes — the command wasn't
malfunctioning subtly, it had simply locked every worker out for the
entire run.

Fix: call `session.commit()` after each read, exactly like
`stop_workers`. Added `tests/test_cli.py::test_benchmark_completes_a_small_batch`
as a regression test — it fails loudly (0 jobs completed, not a timing
fluke) if this pattern gets reintroduced.

The lesson from having now hit this exact shape of bug twice: a
polling/wait loop is the one pattern in this codebase that's easy to get
wrong with a single global `BEGIN IMMEDIATE` hook, precisely because nothing
about `count_jobs()` or `list_jobs()` *looks* like it should need a
commit — they're plain reads. Any new polling loop added later should be
checked against this specifically.
