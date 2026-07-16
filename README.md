# queuectl

A CLI-based background job queue system: enqueue jobs, run multiple worker
processes against them in parallel, retry failures with exponential
backoff, and move permanently-failed jobs to a Dead Letter Queue (DLQ).
Job state is persisted in SQLite, so everything survives a restart.

## 1. Setup

Requires Python 3.9+.

```bash
git clone <your-fork-url>
cd QueueCTL
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -e ".[dev]"
```

This installs a `queuectl` console command (via the `pyproject.toml`
entry point) backed by `click` for the CLI and `SQLAlchemy` as the ORM over
SQLite. `pytest` is installed as a dev dependency.

By default, job/worker/config state lives in `./queuectl_data/queuectl.db`
(created on first use, relative to the current working directory). Override
the location with the `QUEUECTL_DB` environment variable — this is also how
the test suite isolates each test into its own throwaway database.

## 2. Usage examples

```bash
$ queuectl enqueue '{"id":"job1","command":"echo Hello World"}'
Enqueued job job1 (state=pending, max_retries=3)

$ queuectl enqueue '{"command":"exit 1","max_retries":2}'
Enqueued job a1b2c3d4e5f6 (state=pending, max_retries=2)

$ queuectl worker start --count 3
Started 3 worker(s): 3f9a1c2b4d5e, 7b2e8f1a9c3d, 0d4f6a2e8b1c

$ queuectl status
Total jobs: 2
  pending    0
  processing 0
  completed  1
  failed     0
  dead       1

Attempts logged: 3  Success rate: 33.3%

Workers:
  3f9a1c2b4d5e   pid=41232   status=running  current_job=-              last_heartbeat=2026-07-16 10:02:11.123456
  7b2e8f1a9c3d   pid=41233   status=running  current_job=-              last_heartbeat=2026-07-16 10:02:11.201112
  0d4f6a2e8b1c   pid=41234   status=running  current_job=-              last_heartbeat=2026-07-16 10:02:11.302981

$ queuectl list --state completed
job1           completed  attempts=1/3   prio=0   cmd='echo Hello World'

$ queuectl dlq list
a1b2c3d4e5f6   dead       attempts=2/2   prio=0   cmd='exit 1' last_error='exit code 1'

$ queuectl dlq retry a1b2c3d4e5f6
Job a1b2c3d4e5f6 requeued (state=pending)

$ queuectl config set max-retries 5
Set max-retries = 5

$ queuectl config list
max_retries = 5
backoff_base = 2
poll_interval = 1
heartbeat_interval = 2

$ queuectl worker stop
Stop requested for 3 worker(s); 3 confirmed stopped.
```

**Windows note**: job commands run through `cmd.exe` (via `shell=True`),
which has no built-in `sleep`. The examples above and the assignment's
`sleep 2` are POSIX-shell idioms; on Windows use something portable instead,
e.g. `python -c "import time; time.sleep(2)"` or PowerShell's
`powershell -c "Start-Sleep 2"`. This is exactly what the test suite and
`scripts/validate_e2e.py` do to stay cross-platform.

### Command reference

| Command | Description |
|---|---|
| `queuectl enqueue '<json>'` | Add a job. Only `command` is required; `id`, `max_retries`, `priority`, `run_at` (ISO timestamp, for delayed jobs), `timeout_seconds` are optional. |
| `queuectl worker start --count N [--foreground]` | Start N worker processes (detached background by default; `--foreground` runs a single worker in this terminal, Ctrl+C to stop). |
| `queuectl worker stop [--timeout SEC]` | Ask all running workers to finish their current job and exit; waits up to `--timeout` seconds for confirmation. |
| `queuectl status` | Job counts by state, attempt/success stats, and active worker list. |
| `queuectl list [--state STATE] [--limit N]` | List jobs, optionally filtered by state. |
| `queuectl dlq list` | List jobs in the Dead Letter Queue. |
| `queuectl dlq retry <job_id>` | Reset a DLQ job back to `pending` (attempts reset to 0). |
| `queuectl config set/get/list/reset` | Manage `max-retries`, `backoff-base`, `poll-interval`, `heartbeat-interval`, `timeout`. `reset [key]` restores one key (or all, if omitted) back to its default. |

All commands support `--help`.

## 3. Architecture overview

**Layering**: `cli.py` (Click commands) → `queue_ops.py` / `config.py`
(the repository layer — the *only* place that writes ORM queries, all
operating on a SQLAlchemy `Session`) → `validators.py` (pure field
validation, no DB access) / `models.py` (declarative ORM models) →
`database.py` (engine + session factory) → SQLite, with `constants.py`
(state names, default config values) and `exceptions.py` (the
`QueueCTLError` hierarchy) used across every layer. Each layer has one
job: the CLI parses args, formats output, and translates `QueueCTLError`s
into `click.ClickException`; `queue_ops.py` owns every state transition
and is the only module allowed to touch `Session.query`/`add`/`delete`;
`validators.py` rejects bad input before it reaches the database;
`models.py` is pure schema; `database.py` is the only place that knows
about SQLite/SQLAlchemy engine wiring.

**Repository layer** (`queue_ops.py`): generic CRUD —
`create_job`, `get_job`, `list_jobs`, `update_job`, `delete_job`,
`job_exists`, `get_pending_jobs` — plus the job-lifecycle operations that
encode the actual state machine: `claim_job`, `complete_job`, `fail_job`,
`dlq_list`, `dlq_retry`. `update_job` deliberately only allows editing
`command`/`max_retries`/`priority`/`run_at`/`timeout_seconds` — lifecycle
fields (`state`, `attempts`, `next_retry`, `worker_id`, `last_error`) are
only ever changed by the lifecycle functions, so there's exactly one code
path that can move a job between states. **Config repository**
(`config.py`): `get_config`/`set_config`/`reset_config`/`get_all`, plus
`load_defaults` which seeds the `config` table with every known default
key (idempotent — called on every `database.get_session()`), so `queuectl
config list` always shows the complete set of tunables even before
anything has been overridden.

**Validation & errors**: `validators.py` checks each job field in
isolation (non-empty command, `max_retries >= 1`, `priority >= 0`,
positive `timeout_seconds`, parseable `run_at`) and raises
`InvalidJobDataError` on the first failure; duplicate-id checking lives in
`queue_ops.create_job` instead (via `job_exists`) since it needs a
database read, which validators.py deliberately never does. All
queuectl-specific failures subclass `exceptions.QueueCTLError` —
`JobNotFoundError`, `DuplicateJobError`, `InvalidJobDataError`,
`InvalidJobStateError` (e.g. `dlq retry` on a job that isn't dead),
`InvalidConfiguration`, `DatabaseError` — so `cli.py` can catch the single
base class and print a clean message instead of leaking a raw
`ValueError`/`KeyError`/SQLAlchemy traceback.

**Job lifecycle**: `pending` → `processing` → `completed` | `failed` → (`pending` again, after backoff) | `dead`.

- `pending`: eligible to be claimed by any worker.
- `processing`: claimed by exactly one worker, currently executing.
- `completed`: exit code 0.
- `failed`: exit code non-zero, retries remain; `next_retry` set to `now + backoff_base^attempts`.
- `dead`: exit code non-zero, retries exhausted (`attempts >= max_retries`) — this **is** the DLQ; there's no separate table, just a `state='dead'` filter, so `dlq list`/`dlq retry` are thin wrappers over the same `jobs` table.

**Data persistence**: a single SQLite database (`queuectl_data/queuectl.db`
by default) in WAL mode, modeled as four SQLAlchemy ORM classes in
`models.py`:
- `Job` (table `jobs`) — the queue itself: `id`, `command`, `state`,
  `attempts`, `max_retries`, `created_at`, `updated_at`, `next_retry`, plus
  bonus-feature columns `priority`, `run_at` (scheduled jobs),
  `timeout_seconds`, `worker_id`, `last_error`.
- `Config` (table `config`) — key/value store for `max_retries`,
  `backoff_base`, etc., with built-in defaults in `config.py` so the table
  can start empty.
- `Worker` (table `workers`) — one row per worker process (`pid`, `status`,
  `stop_requested`, `current_job_id`, heartbeat), so `status` and `worker
  stop` have something to look at across separate CLI invocations. This
  table isn't part of a minimal jobs+config schema, but the assignment
  requires `status` to show active workers and `worker stop` to signal
  them gracefully — both need somewhere durable to read/write across
  process boundaries.
- `JobLog` (table `job_logs`) — one row per execution attempt
  (stdout/stderr/exit code/timing) — job output logging and the
  success-rate stat in `status` are built directly from this table.

```
┌───────────────────────────┐        ┌──────────────────────┐
│ jobs                      │        │ job_logs             │
├───────────────────────────┤        ├──────────────────────┤
│ id            TEXT PK     │◄──┐    │ id           PK      │
│ command       TEXT        │   │    │ job_id       TEXT ───┼──┐
│ state         TEXT        │   └────┼──────────────────────┘  │
│ attempts      INT         │        │ attempt      INT        │
│ max_retries   INT         │        │ stdout       TEXT       │
│ priority      INT         │        │ stderr       TEXT       │
│ run_at        DATETIME    │        │ exit_code    INT        │
│ next_retry    DATETIME    │        │ started_at   DATETIME   │
│ timeout_secs  INT         │        │ finished_at  DATETIME   │
│ worker_id     TEXT        │        └──────────────────────┘  │
│ last_error    TEXT        │  (job_id is a soft reference --  │
│ created_at    DATETIME    │   logs outlive a deleted job     │
│ updated_at    DATETIME    │   for audit purposes) ───────────┘
└───────────────────────────┘

┌───────────────────────────┐        ┌──────────────────────┐
│ workers                   │        │ config               │
├───────────────────────────┤        ├──────────────────────┤
│ worker_id     TEXT PK     │        │ key         TEXT PK  │
│ pid           INT         │        │ value       TEXT     │
│ status        TEXT        │        └──────────────────────┘
│ stop_requested BOOL       │        (max_retries, backoff_base,
│ current_job_id TEXT       │         poll_interval, heartbeat_interval,
│ started_at    DATETIME    │         timeout -- one row per key,
│ last_heartbeat DATETIME   │         seeded by config.load_defaults)
└───────────────────────────┘
```

There's no foreign key from `jobs.worker_id` to `workers.worker_id` or
from `job_logs.job_id` to `jobs.id` — both are intentionally soft
references. A job's logs should stay queryable even if the job row itself
is later deleted via `queue_ops.delete_job` (audit trail), and a worker
row can legitimately outlive the job it last touched (job completes,
worker moves on).

**Worker logic & locking**: `queuectl worker start --count N` spawns N
detached OS processes (`python -m queuectl.worker`), each running an
independent poll loop against the same database file, each with its own
SQLAlchemy `Session`. Job claiming (`queue_ops.claim_job`) does the "find
an eligible job" query and the "mark it processing" update in a single
transaction. Getting SQLite to actually take a write lock *before* that
`SELECT` runs takes two `database.py` engine event hooks, because
pysqlite's default driver behavior fights against it:
- on `connect`, `dbapi_connection.isolation_level = None` disables
  pysqlite's own implicit transaction handling;
- on `begin`, we emit `BEGIN IMMEDIATE` ourselves.

With that in place, every transaction on this engine opens with `BEGIN
IMMEDIATE`, so two workers racing to claim a job are serialized by
SQLite's write lock — neither can see the job as free once the other has
claimed it. This is the mechanism that prevents duplicate execution
(verified in `tests/test_queue_ops.py::test_concurrent_claims_never_double_claim`
with 8 threads racing over 20 jobs, and end-to-end in
`scripts/validate_e2e.py` with 4 real worker processes over 20 jobs). The
trade-off: because *every* transaction takes the write lock, not just
writes, a loop that reads via the ORM without committing between
iterations will hold that lock indefinitely and block other processes —
`worker_manager.stop_workers`'s polling loop commits after every read for
exactly this reason (see design.md).

**Graceful shutdown**: `worker stop` doesn't send OS signals (which behave
inconsistently across platforms, especially Windows) — it sets a
per-worker `stop_requested` flag on that `Worker` row. Each worker only
checks that flag *between* jobs, never mid-execution, so a job that's
already running is always allowed to finish. Because the worker's
SQLAlchemy session expires all objects on commit by default, re-reading
`worker_row.stop_requested` after any commit transparently re-queries the
database, so a flag set by a different process is picked up on the very
next loop iteration. `worker stop` polls the table until workers report
`status='stopped'` (or a timeout elapses). A `--foreground` worker also
responds to Ctrl+C the same way, via a signal handler that sets the same
in-loop stop condition.

**Retry & backoff**: on failure, `attempts` increments and, if retries
remain, the job goes to `failed` with `next_retry = now +
backoff_base ** attempts`. The claim query treats `pending` jobs
and `failed` jobs whose `next_retry` has elapsed as equally eligible —
so there's no separate scheduler/reaper process needed to "wake up" failed
jobs.

**Scheduling & priority (bonus)**: `run_at` (ISO timestamp) on a job makes
it ineligible until that time — this is the `run_at`/delayed-jobs bonus
feature, implemented as one extra `WHERE` clause in the same claim query.
`priority` (higher = claimed first) is an `ORDER BY` on the same query. Job
timeouts (`timeout_seconds`) are enforced via `subprocess.run(timeout=...)`
and treated as an ordinary failure (retryable like any other).

## 4. Assumptions & trade-offs

- **SQLite over JSON files**: SQLite gives real transactional locking
  (`BEGIN IMMEDIATE`) for free, which is exactly what's needed to prevent
  duplicate job claims across worker processes. A hand-rolled JSON + file
  lock would need to reimplement the same guarantee less reliably.
- **SQLAlchemy ORM over raw `sqlite3`**: models are declarative Python
  classes (`Job`, `Config`, `Worker`, `JobLog` in `models.py`) instead of
  hand-written SQL strings, which keeps `queue_ops.py` readable as plain
  attribute access (`job.state = State.DEAD`) and keeps schema changes to
  one place. The one place this costs extra care is the claim-locking
  guarantee above: SQLAlchemy's default SQLite driver behavior doesn't
  naturally support `BEGIN IMMEDIATE`, so `database.py` has to disable
  pysqlite's implicit transactions and re-emit it manually via engine
  events (documented inline there and in design.md).
- **Workers are separate OS processes, not threads**: this mirrors how a
  real job queue would be deployed (independent, crash-isolated workers)
  and sidesteps the GIL for CPU-bound job commands; the cost is that
  `worker start` returns once processes are spawned rather than blocking,
  so worker liveness is tracked via a DB row + heartbeat rather than an
  in-memory handle.
- **Stop is cooperative, not signal-based**: chosen for cross-platform
  consistency (this was developed/tested on Windows, where POSIX signal
  semantics don't fully apply). The trade-off is a worker can take up to
  one `poll_interval` to notice a stop request while idle — acceptable
  since jobs themselves are never interrupted mid-run either way.
- **`command` runs via `shell=True`**: matches the assignment's examples
  (`sleep 2`, `echo hello`) which are shell idioms, not literal
  executables. This does mean job commands are trusted input — this tool
  assumes jobs are enqueued by a trusted operator/system, the same
  assumption any cron-like job runner makes.
- **DLQ is a view, not a separate store**: `dead` is just another job
  state, so DLQ history, retries, and stats all reuse the same schema and
  code paths instead of duplicating them.
- **No distributed/multi-host coordination**: everything assumes a single
  shared SQLite file on one machine (as scoped by the assignment); it does
  not attempt multi-host queue semantics.
- **`timeout` config default exists but isn't auto-applied**: `constants.DEFAULT_TIMEOUT`
  (30s) and a `timeout` config key exist for consistency with `max_retries`/
  `backoff_base`, but a job's `timeout_seconds` still defaults to `None`
  (no timeout) unless the job explicitly sets it — unlike `max_retries`,
  which *does* fall back to the config default. Silently capping every
  job at 30s by default felt like a surprising behavior change for a
  feature the assignment lists as optional; explicit opt-in avoids
  breaking a legitimately long-running job that never asked for a limit.

## 5. Testing instructions

Unit tests (fast, no real subprocess/worker spawning — job execution is
exercised via constructed `ExecutionResult`s, and the concurrency guarantee
is tested with real threads against a real SQLite file):

```bash
pytest
```

End-to-end validation (spawns the real CLI and real worker processes,
covering the assignment's 5 required scenarios: basic completion,
retry→backoff→DLQ, parallel workers with no duplicate execution, invalid
commands failing gracefully, and persistence across a restart):

```bash
python scripts/validate_e2e.py
```

Repository-layer integration check (calls `queue_ops.py`/`config.py`
directly against a real SQLite file — Create → Read → Update → Delete,
duplicate/missing-id errors, and a config override surviving a simulated
restart):

```bash
python scripts/validate_db.py
```

Manual smoke test:

```bash
queuectl enqueue '{"id":"ok","command":"echo hi"}'
queuectl enqueue '{"id":"bad","command":"exit 1","max_retries":2}'
queuectl worker start --count 2
queuectl status        # watch counts change
queuectl list --state dead
queuectl worker stop
# close the terminal / open a new one, then:
queuectl list           # jobs are still there
```

## Demo

<!-- Add the recorded CLI demo link here before submitting, per the assignment's Submission section. -->

## Bonus features implemented

- Job timeout handling (`timeout_seconds` per job)
- Job priority queues (`priority` field, higher first)
- Scheduled/delayed jobs (`run_at`)
- Job output logging (`job_logs` table: stdout/stderr/exit code per attempt)
- Execution stats (attempts logged + success rate in `queuectl status`)
