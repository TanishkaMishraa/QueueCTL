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
entry point) backed by `click` for the CLI, `rich` for formatted output
(panels/tables), and `SQLAlchemy` as the ORM over SQLite. `pytest` is
installed as a dev dependency.

By default, job/worker/config state lives in `./queuectl_data/queuectl.db`
(created on first use, relative to the current working directory). Override
the location with the `QUEUECTL_DB` environment variable — this is also how
the test suite isolates each test into its own throwaway database.

## 2. Usage examples

```bash
$ queuectl enqueue "echo Hello World" --priority high --max-retries 5
+-- Job Created ------+
| ID: 6362703482c3    |
| State: pending      |
| Max Retries: 5      |
| Priority: 10        |
+----------------------+

$ queuectl enqueue '{"id":"job2","command":"exit 1","max_retries":2}'
+- Job Created --+
| ID: job2       |
| State: pending |
| Max Retries: 2 |
+----------------+

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

Workers Running: 3
  3f9a1c2b4d5e   pid=41232   status=running  current_job=-              heartbeat=1s ago
  7b2e8f1a9c3d   pid=41233   status=running  current_job=-              heartbeat=1s ago
  0d4f6a2e8b1c   pid=41234   status=running  current_job=-              heartbeat=2s ago

$ queuectl worker list
# ...some time later, after one worker process was killed/crashed:
Workers Running: 3
  3f9a1c2b4d5e   pid=41232   status=running  current_job=-              heartbeat=3s ago
  7b2e8f1a9c3d   pid=41233   status=running  current_job=-              heartbeat=3s ago
  0d4f6a2e8b1c   pid=41234   status=running  current_job=-              heartbeat=47s ago  [STALE - no heartbeat, likely crashed]

$ queuectl list --state completed
+-----------------------------------------------------------------+
| ID           | State     | Attempts | Priority | Command        |
|--------------+-----------+----------+----------+----------------|
| job1         | completed | 1/3      | 0        | echo Hello ... |
+-----------------------------------------------------------------+

$ queuectl job show job2
+------------- Job job2 --------------+
| ID: job2                            |
| Command: exit 1                     |
| State: dead                         |
| Attempts: 2/2                       |
| Priority: 0                         |
| Created: 2026-07-16 19:28:32.887758 |
| Updated: 2026-07-16 19:28:33.912004 |
| Last Error: exit code 1             |
+--------------------------------------+

$ queuectl job delete job2
Delete job job2? [y/N]: y
Deleted job job2

$ queuectl dlq list
a1b2c3d4e5f6   dead       attempts=2/2   prio=0   cmd='exit 1' last_error='exit code 1'

$ queuectl dlq count
Dead Jobs: 1

$ queuectl dlq retry a1b2c3d4e5f6
Job a1b2c3d4e5f6 requeued (state=pending)

$ queuectl dlq delete a1b2c3d4e5f6
Delete job a1b2c3d4e5f6 permanently? [y/N]: y
Deleted job a1b2c3d4e5f6

$ queuectl config set max-retries 5
Set max-retries = 5

$ queuectl config set backoff-base 0.5
Error: backoff_base must be > 1

$ queuectl config show
max_retries = 5
backoff_base = 2
poll_interval = 1
heartbeat_interval = 2
timeout = 30
default_priority = 0
max_workers = 10

$ queuectl config export backup.json
Exported configuration to backup.json

$ queuectl config delete max-retries
Deleted override for max-retries (back to default)

$ queuectl stats
Jobs Executed
  Completed : 95
  Failed    : 8
  Retries   : 12
  DLQ       : 4

Average Runtime : 1.84 sec
Longest Runtime : 6.20 sec
Shortest Runtime: 0.10 sec

Success Rate: 92.2%
Failure Rate: 7.8%
Retry Rate  : 11.7%
Throughput  : 47.5 jobs/hour

$ queuectl health
Database
  [OK] Healthy

Workers
  [OK] Running (3 active)

Queue
  [OK] Accepting Jobs

DLQ
  4 Job(s)

Configuration
  [OK] Loaded

$ queuectl worker stop
Stop requested for 3 worker(s); 3 confirmed stopped.
```

(Panels/tables above are Rich output — actual box-drawing characters vary
slightly by terminal; box-drawing falls back to plain ASCII automatically
on legacy Windows consoles, which is also why panel titles avoid non-ASCII
symbols like a checkmark glyph — that specific combination crashes with a
`UnicodeEncodeError` under the default Windows `cp1252` codepage, a bug
caught only by running the real CLI, not by `CliRunner`-based tests, since
captured test output never goes through that console code path.)

**Windows note**: job commands run through `cmd.exe` (via `shell=True`),
which has no built-in `sleep`. The examples above and the assignment's
`sleep 2` are POSIX-shell idioms; on Windows use something portable instead,
e.g. `python -c "import time; time.sleep(2)"` or PowerShell's
`powershell -c "Start-Sleep 2"`. This is exactly what the test suite and
`scripts/validate_e2e.py` do to stay cross-platform.

### Command reference

| Command | Description |
|---|---|
| `queuectl enqueue "<command>" [--id ID] [--priority P] [--timeout SEC] [--max-retries N] [--run-at ISO]` | Add a job from a plain shell command. `--priority` accepts an integer or `low`/`normal`/`high`. |
| `queuectl enqueue '<json>'` | Same command, JSON form — matches the assignment's original example (`{"id":"job1","command":"sleep 2"}`). Detected automatically: if the argument starts with `{` it's parsed as JSON, otherwise it's treated as a literal command. |
| `queuectl worker start --count N [--foreground]` | Start N worker processes (detached background by default; `--foreground` runs a single worker in this terminal, Ctrl+C to stop). |
| `queuectl worker stop [--timeout SEC]` | Ask all running workers to finish their current job and exit; waits up to `--timeout` seconds for confirmation. |
| `queuectl worker list` | List worker processes with PID, status, current job, and heartbeat age; flags any with a stale heartbeat. A focused view of what `status` also shows. |
| `queuectl status` | Job counts by state, attempt/success stats, and active worker list. |
| `queuectl stats` | Execution statistics: completed/failed/retries/DLQ counts, average/longest/shortest runtime, success/failure/retry rate, throughput. |
| `queuectl health` | Checks database connectivity, worker availability, queue accessibility, and configuration; `[OK]`/`[WARN]`/`[FAIL]` per check. |
| `queuectl dashboard` | Rich multi-panel view: queue stats, current workers, recent jobs, DLQ health — a visual superset of `status`, which stays plain-text/scriptable. |
| `queuectl list [--state STATE] [--limit N]` | List jobs (Rich table), optionally filtered by state. |
| `queuectl job show <job_id>` | Show full details for one job (command, state, attempts, timestamps, last error). |
| `queuectl job delete <job_id> [--yes]` | Delete a job permanently; prompts for confirmation unless `--yes`/`-y` is given. |
| `queuectl dlq list` | List jobs in the Dead Letter Queue. |
| `queuectl dlq count` | Show how many jobs are currently in the DLQ. |
| `queuectl dlq retry <job_id>` | Reset a DLQ job back to `pending` (attempts and `next_retry` cleared). |
| `queuectl dlq delete <job_id> [--yes]` | Permanently delete a job, but only if it's actually in the DLQ; prompts for confirmation unless `--yes`/`-y`. Rejects a job that isn't dead (use `queuectl job delete` for that). |
| `queuectl config set/get/list/show/reset/delete` | Manage `max-retries`, `backoff-base`, `poll-interval`, `heartbeat-interval`, `timeout`, `default-priority`, `max-workers`. `show` is an alias for `list`; `delete <key>` and `reset <key>` both restore that one key's default; `reset` with no key restores everything. |
| `queuectl config export <file>` / `queuectl config import <file>` | Write all config values to a JSON file, or load them back from one (e.g. to replicate settings across a deployment). |
| `queuectl benchmark --jobs N --workers W [--timeout SEC]` | Optional/bonus performance test: enqueues N trivial jobs, starts W fresh workers, waits for the batch to finish, then reports completion count, average runtime, and jobs/sec throughput. Starts and stops its own workers, independent of any already running. |

All commands support `--help`.

**Not implemented**: `queuectl job search <term>` — the phase notes this
one as a "future feature" rather than a requirement, and it isn't part of
the assignment's own command list, so it's left out for now rather than
adding CLI surface nothing else depends on. `queue_ops.list_jobs` would be
the natural place to add a `command LIKE %term%` filter if it's wanted
later.

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
`dlq_list`, `dlq_retry`. There's no separate "job service" layer between
`cli.py` and `queue_ops.py`: `create_job` already generates the id (if not
given), timestamps, and default state/retries and returns the `Job`
object directly, and `cli.py` already calls it instead of touching
SQLAlchemy directly — a pass-through service module in between would just
forward every call unchanged. `update_job` deliberately only allows editing
`command`/`max_retries`/`priority`/`run_at`/`timeout_seconds` — lifecycle
fields (`state`, `attempts`, `next_retry`, `worker_id`, `last_error`) are
only ever changed by the lifecycle functions, so there's exactly one code
path that can move a job between states. **Config repository**
(`config.py`): `get_config`/`set_config`/`get_all`/`exists`/`delete`/
`reset_config`/`export_config`/`import_config`, plus `load_defaults`
which seeds the `config` table with every known default key (idempotent
— called on every `database.get_session()`), so `queuectl config show`
always shows the complete set of tunables even before anything has been
overridden. Every known key (`max_retries`, `backoff_base`,
`poll_interval`, `heartbeat_interval`, `timeout`, `default_priority`,
`max_workers`) has a validator in `config._VALIDATORS`, run by
`set_config` before anything is written — `backoff_base` must be `> 1`
(a base of 1 or less would never grow the retry delay), the interval/
timeout/`max_workers` keys must be positive, `max_retries`/
`default_priority` must be non-negative — and `set_config` also rejects
unknown keys outright rather than silently storing them. `poll_interval`
and `backoff_base` are read fresh from config on every worker loop
iteration (not cached at worker startup), so `queuectl config set
poll-interval 5` takes effect on already-running workers within one
cycle; `max_retries`/`priority` are captured once per job at enqueue
time instead, since changing an in-flight job's already-decided retry
budget out from under it would be surprising, not useful.
`export_config`/`import_config` round-trip the table through a JSON file
of numbers (not the raw strings stored internally) via `queuectl config
export/import <file>`, validating every key before writing anything on
import so a bad file can't partially apply.

**Validation & errors**: `validators.py` checks each job field in
isolation (non-empty command, `max_retries >= 0` — a job created with
`max_retries=0` is intentionally allowed and means "no retries, straight
to the DLQ on first failure", `priority >= 0`,
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

**Dead Letter Queue** (`dlq.py`): `list_dead_jobs`, `count_dead_jobs`,
`retry_dead_job`, `delete_dead_job` — a dedicated namespace for DLQ
operations, matching how `cli.py`'s `dlq` command group calls them (`dlq
list` → `list_dead_jobs`, `dlq count` → `count_dead_jobs`, etc.). None of
these functions write their own queries, though: `dlq.py` delegates to
`queue_ops.py` (via `list_jobs`, `count_jobs`, `dlq_retry`, `get_job` +
`delete_job`) rather than becoming a second module that talks to
`Session` directly, so "the only module that writes ORM queries" stays
true even with DLQ operations pulled into their own file. The one place
`dlq.py` adds real logic beyond delegation is `delete_dead_job`, which
checks `job.state == State.DEAD` before allowing the delete — unlike the
generic `queuectl job delete` (any state), `queuectl dlq delete` refuses
to touch a job that isn't actually in the DLQ. A job never reaches `dead`
except through `queue_ops.fail_job` deciding `retry.is_dead(...)` is true
— there's no separate `move_to_dlq(job_id)` entry point, since "died as a
direct result of this failed attempt" is one atomic transition (update
state + store the error + log the attempt + commit), not two calls a
caller could invoke out of order or forget to pair up. Both `dlq retry`
and `dlq delete` append an event to `logs/worker.log` (manually retried /
deleted from DLQ), alongside the automatic "exceeded retries, moved to
DLQ" event a worker logs when it happens.

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

**Crash resilience & heartbeat staleness**: workers are independent OS
processes with no supervisor watching them, so one worker being killed
(crash, forced termination, OOM, whatever) has zero effect on the others —
each just keeps polling the shared database on its own. The trade-off is
that a crashed worker's `workers` row is stuck at `status='running'`
forever, since "mark myself stopped" is code that runs in the worker's own
shutdown path, which a crash never reaches. That's what
`constants.HEARTBEAT_STALE_SECONDS` (30s) is for: `queuectl status` and
`queuectl worker list` compute each running worker's heartbeat age at
display time and tag it `[STALE - no heartbeat, likely crashed]` once it
exceeds that threshold, rather than trusting the possibly-stuck `status`
column on its own. This is a purely computed, read-time flag — nothing
writes it back to the database (a worker that was just slow, not dead,
shouldn't have its row silently rewritten from a `status` read). Verified
end-to-end in `scripts/validate_e2e.py`'s crash scenario: 12 jobs, 3
workers, one worker force-killed mid-batch (`SIGKILL` on POSIX;
`os.kill(pid, SIGTERM)` on Windows, which CPython maps directly to
`TerminateProcess` — neither is deliverable to a signal handler, so it's
a true crash, not a graceful shutdown) — the other two finish all 12 jobs
with no job executed twice, and the killed worker's row is confirmed to
still read `status='running'`.

**Retry & backoff**: on failure, `attempts` increments and, if retries
remain, the job goes to `failed` with `next_retry = now +
backoff_base ** attempts`. The claim query treats `pending` jobs
and `failed` jobs whose `next_retry` has elapsed as equally eligible —
so there's no separate scheduler/reaper process needed to "wake up" failed
jobs. The backoff math and the dead/retry decision themselves live in
`retry.py` as two small, pure functions — `calculate_delay(attempts, base)`
and `is_dead(attempts, max_retries)` — deliberately kept independent of
the database so the exact delay sequence (2s/4s/8s/16s for `base=2`) and
the retry-limit boundary are unit-tested directly (`tests/test_retry.py`)
rather than only indirectly through a full `fail_job` call. `queue_ops.fail_job`
calls both and applies the result to the `Job` row; the worker loop never
computes a delay or a dead/alive decision itself, it only calls
`fail_job` and logs whatever it decided.

**Job execution** (`executor.py`, renamed from an earlier `execution.py`
to match the "command executor" terminology): `run_command(command,
timeout_seconds)` runs the job's command via `shell=True`, capturing
`stdout`, `stderr`, `exit_code`, and `duration_seconds` (wall-clock time
around the call) into an `ExecutionResult`. Exit code 0 is the only
definition of success; everything else (non-zero exit, a shell "command
not found", or a timeout) is a normal, retryable failure — there's no
separate code path for "the command didn't exist" versus "the command ran
and returned 1". Internally this is `subprocess.Popen` +
`proc.communicate(timeout=...)` rather than the simpler
`subprocess.run(..., timeout=...)` — see the trade-offs section below for
why that distinction actually matters here, not just style.

**Logging** (`app_logging.py`; override the directory with
`QUEUECTL_LOG_DIR`, default `./logs`): three plain-text files, all via
Python's standard `logging` module —
- `worker.log` — each worker process's lifecycle: started, picked up job
  X, completed in N seconds, failed/retry-in-Ns, exceeded retries → DLQ,
  stopped.
- `queuectl.log` — general CLI-driven events: job created, job deleted,
  config changed, DLQ manually retried/deleted.
- `error.log` — every ERROR-level record logged anywhere (currently just
  "exceeded retries, moved to DLQ"), collected in one place for quick
  triage.

`error.log`'s contents aren't a separate, manually-duplicated log call:
`get_app_logger()`/`get_worker_logger()` return children of a `"queuectl"`
root logger, and a child logger's records propagate up to that root by
default. The root's own handler is filtered to `level=ERROR`, so calling
`.error(...)` on either child logger writes to that child's own file
*and* error.log, while `.info()`/`.warning()` calls propagate too but get
filtered out by the root handler's level before they'd reach error.log.
One call site, two files, with no explicit "also log this as an error"
second call needed.

This is all separate from the `job_logs` database table: `job_logs` is
structured, queryable, per-attempt stdout/stderr/exit-code data (the job
output logging bonus feature); the three log files above are plain-text
operational logs you'd tail while a queue is running, e.g. to record the
demo video (`scripts/worker_demo.py` prints where they live).

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
  in-memory handle. `worker_manager._spawn_detached` explicitly passes
  `cwd=os.getcwd()` to `Popen` rather than relying on the child inheriting
  the parent's working directory implicitly — on Windows, a process
  launched with `DETACHED_PROCESS` isn't guaranteed to end up with the
  same effective current directory as its parent otherwise, which
  (observed on a real machine, not just in testing) let a worker silently
  read/write a *different* `queuectl_data/queuectl.db` than the CLI
  commands checking on it — see design.md for the full writeup.
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
- **Job timeouts kill the whole process tree, not just the shell**: with
  `shell=True`, the process actually started is `cmd.exe`/`sh -c`
  wrapping the real command as a *grandchild*. `subprocess.run(...,
  timeout=...)`'s built-in handling only kills that direct child, leaving
  the grandchild running to completion in the background — on Windows,
  the call doesn't even return in time, because `communicate()` stays
  blocked on stdout/stderr pipes the orphaned grandchild still holds open.
  Confirmed directly: a `time.sleep(10)` job with `timeout_seconds=1` took
  ~10s to return under the naive approach. `executor.run_command` uses
  `Popen` + a manual `TimeoutExpired` handler that kills the *tree*
  (`taskkill /F /T /PID` on Windows, `os.killpg` on POSIX via
  `start_new_session=True`) instead of just the immediate child — see
  design.md for the full writeup and `tests/test_executor.py` for the
  regression test.

## 5. Testing instructions

Unit tests — one file per module (`test_database.py`, `test_queue_ops.py`,
`test_executor.py`, `test_retry.py`, `test_dlq.py`, `test_worker.py`,
`test_config.py`, `test_metrics.py`, `test_app_logging.py`,
`test_worker_manager.py`, `test_cli.py`), 122 tests total. Mostly fast and
mocking-free — real SQLite files, real threads for the concurrency
guarantee, real subprocesses in `test_executor.py`'s timeout test — with
`test_worker.py` the one exception that runs the actual `worker.run()`
loop in a background thread (signal registration mocked out, since
`signal.signal` only works on the main thread) rather than spawning a
real OS process, which `scripts/validate_e2e.py` already covers:

```bash
pytest
```

End-to-end validation (spawns the real CLI and real worker processes,
covering the assignment's 5 required scenarios — basic completion,
retry→backoff→DLQ, parallel workers with no duplicate execution, invalid
commands failing gracefully, and persistence across a restart — plus 2
supplementary ones: graceful shutdown actually waits for an in-flight job
to finish, and force-killing one worker out of several doesn't stop the
others or cause any job to run twice):

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

Live worker demo (enqueues a varied batch — success, failure→retry→DLQ,
priority, invalid command — starts 2 workers, and prints `status` every 2
seconds for 10 seconds so you can watch state transitions happen; this is
what recording the CLI demo video against is meant to look like):

```bash
python scripts/worker_demo.py
```

Performance benchmark (optional/bonus — not part of the assignment's
required test scenarios):

```bash
queuectl benchmark --jobs 100 --workers 4
```

### Continuous Integration

`.github/workflows/ci.yml` runs on every push/PR to `main`: installs the
package, checks formatting (`black --check`) and import order
(`isort --check-only`), then runs `pytest`, `scripts/validate_db.py`, and
`scripts/validate_e2e.py` — on a matrix of Ubuntu and Windows runners
across two Python versions, so both the Windows-specific code paths
(`taskkill`-based process tree kill, the `cwd`-pinning fix) and the POSIX
ones (`os.killpg`, `SIGKILL`) actually get exercised in CI, not just
locally.

### Packaging

```bash
pip install -e ".[dev]"   # editable install + dev tools (pytest, black, isort)
black queuectl tests scripts     # format
isort queuectl tests scripts     # sort imports
```

A `Dockerfile` is included as an optional packaging target (not required
by the assignment): `docker build -t queuectl .` then
`docker run --rm -v queuectl-data:/data -e QUEUECTL_DB=/data/queuectl.db queuectl enqueue "echo hi"`.
Since a container has no shell left around to keep a *detached* background
process alive, a long-running worker container needs `--foreground`:
`docker run -d -v queuectl-data:/data -e QUEUECTL_DB=/data/queuectl.db queuectl worker start --count 1 --foreground`.

## Demo

[Project demo video](https://drive.google.com/file/d/1aV68kVGea_eJX5e3vk5XoNoSCDns161z/view?usp=drive_link)

## Bonus features implemented

- Job timeout handling (`timeout_seconds` per job)
- Job priority queues (`priority` field, higher first; `default_priority` configurable)
- Scheduled/delayed jobs (`run_at`)
- Job output logging (`job_logs` table: stdout/stderr/exit code/duration per attempt)
- Operational logging (`logs/worker.log`, `logs/queuectl.log`, `logs/error.log`)
- Execution stats & metrics (`queuectl stats`: success/failure/retry rate, runtime, throughput)
- Queue health check (`queuectl health`)
- Rich dashboard (`queuectl dashboard`)
- Config import/export (`queuectl config export/import <file>`), per-key validation, and a worker-count cap (`max_workers`)
- Performance benchmark (`queuectl benchmark --jobs N --workers W`)
- Packaging: `Dockerfile`, GitHub Actions CI (`.github/workflows/ci.yml`)

## Future improvements

Out of scope for this assignment, but the natural next steps if this
grew into a real deployment rather than a single-machine CLI tool:

- **Redis or PostgreSQL backend**: SQLite's single-writer model (the
  `BEGIN IMMEDIATE` locking this project relies on) tops out well before
  a high-throughput, multi-host queue would need. A Redis-backed queue
  (e.g. atomic `RPOPLPUSH`/Lua-script claiming) or PostgreSQL with
  `SELECT ... FOR UPDATE SKIP LOCKED` would remove the single-file,
  single-machine ceiling entirely.
- **REST API**: a thin FastAPI/Flask layer over `queue_ops.py`/`dlq.py`
  (already structured as the only modules that touch the database) would
  expose enqueue/status/DLQ operations to non-CLI clients without
  duplicating any business logic.
- **Distributed workers / Kubernetes**: workers are already independent,
  crash-isolated OS processes with no shared in-memory state — the main
  blocker to running them across multiple machines is purely the SQLite
  file being local-disk-only. Swapping in the Redis/PostgreSQL backend
  above would make a Kubernetes `Deployment` of worker pods (readiness
  probes hitting `queuectl health`, `HorizontalPodAutoscaler` on queue
  depth) a fairly direct extension of the existing worker loop.
- **Web dashboard**: `queuectl dashboard` already renders a Rich terminal
  view from the same `queue_ops.status_summary`/`dlq.count_dead_jobs`/
  `metrics.calculate_metrics` calls a web UI would use — a small
  Flask/FastAPI + HTMX or React frontend could reuse that data layer
  as-is rather than needing new query logic.
