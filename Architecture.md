# 🏗️ QueueCTL — Architecture

> Deep-dive into the system design, module boundaries, data flow, and concurrency model.  
> For usage and CLI reference, see the main [README.md](README.md).  
> For the "why" behind specific bugs and decisions, see [design.md](design.md).

---

## Table of Contents

- [High-Level Overview](#high-level-overview)
- [System Layers](#system-layers)
  - [CLI Layer](#1-cli-layer)
  - [Repository Layer](#2-repository-layer)
  - [Validation & Models](#3-validation--models)
  - [Database Engine](#4-database-engine)
- [Module Dependency Graph](#module-dependency-graph)
- [Module Reference](#module-reference)
- [Worker Architecture](#worker-architecture)
  - [Process Model](#process-model)
  - [Worker Lifecycle](#worker-lifecycle)
  - [Command Execution & Timeout Handling](#command-execution--timeout-handling)
- [Job Lifecycle State Machine](#job-lifecycle-state-machine)
  - [State Transitions](#state-transitions)
  - [Claim Eligibility Rules](#claim-eligibility-rules)
- [Concurrency & Locking Model](#concurrency--locking-model)
  - [BEGIN IMMEDIATE Semantics](#begin-immediate-semantics)
  - [SQLAlchemy Integration](#sqlalchemy-integration)
  - [Polling Loop Safety Rule](#polling-loop-safety-rule)
- [Database Schema](#database-schema)
  - [Entity-Relationship Diagram](#entity-relationship-diagram)
  - [Table Details](#table-details)
  - [Soft References (No Foreign Keys)](#soft-references-no-foreign-keys)
- [Error Handling Strategy](#error-handling-strategy)
- [Retry & Backoff Model](#retry--backoff-model)
- [Configuration Architecture](#configuration-architecture)
- [Logging Architecture](#logging-architecture)
- [Testing Architecture](#testing-architecture)
- [Cross-Platform Considerations](#cross-platform-considerations)
- [Directory Structure](#directory-structure)

---

## High-Level Overview

QueueCTL is a **CLI-based background job queue** that persists all state in a single SQLite database file. It uses **independent OS processes** as workers, **`BEGIN IMMEDIATE` transactions** for mutual exclusion, and a **five-state job lifecycle** with exponential backoff and a built-in Dead Letter Queue.

```
┌──────────────────────────────────────────────────────────────┐
│                      User (Terminal)                         │
└──────────────────────┬───────────────────────────────────────┘
                       │  queuectl <command>
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                 CLI Layer  (cli.py)                           │
│     Click commands · Rich output · Error translation         │
└──────────────────────┬───────────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────────┐
│             Repository Layer                                  │
│   queue_ops.py · config.py · dlq.py · metrics.py              │
│   ─── the ONLY modules that write ORM queries ───             │
└──────────────────────┬───────────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────────┐
│          Validation & Models                                  │
│   validators.py  (pure field checks, no DB)                   │
│   models.py      (declarative ORM: Job, Config, Worker, Log)  │
│   exceptions.py  (QueueCTLError hierarchy)                    │
│   constants.py   (state names, defaults, thresholds)          │
└──────────────────────┬───────────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────────┐
│             Database Engine  (database.py)                    │
│      SQLAlchemy · BEGIN IMMEDIATE · WAL mode · busy_timeout   │
└──────────────────────┬───────────────────────────────────────┘
                       │
                  ┌────▼────┐
                  │  SQLite  │
                  └─────────┘
```

---

## System Layers

### 1. CLI Layer

**Module:** [`cli.py`](queuectl/cli.py)

The outermost shell — Click command groups that parse arguments, call into the repository layer, and render results with Rich panels/tables.

**Responsibilities:**
- Parse and validate CLI arguments (Click decorators)
- Translate `QueueCTLError` subclasses → `click.ClickException` for user-friendly error messages
- Format output using Rich panels, tables, and the multi-panel dashboard
- Never contains business logic or SQL queries

**Key pattern:** All output strings are built _inside_ the `try` block while the session is still open, then printed after. This avoids `DetachedInstanceError` from SQLAlchemy's expire-on-commit behavior.

---

### 2. Repository Layer

**Modules:** [`queue_ops.py`](queuectl/queue_ops.py) · [`config.py`](queuectl/config.py) · [`dlq.py`](queuectl/dlq.py) · [`metrics.py`](queuectl/metrics.py)

The **only** modules that write ORM queries (`session.query`, `session.add`, `session.delete`, `session.commit`).

| Module | Scope |
|---|---|
| `queue_ops.py` | All job CRUD + lifecycle transitions (`claim_job`, `complete_job`, `fail_job`, `dlq_retry`) + worker/log queries |
| `config.py` | Config CRUD with per-key validation, `load_defaults` seeds on first use, typed getters (`get_int`, `get_float`) |
| `dlq.py` | DLQ convenience operations — delegates to `queue_ops.py`, never writes its own queries |
| `metrics.py` | Computes aggregated stats from `job_logs` — delegates to `queue_ops.py` for raw data |

**Design rule:** `dlq.py` and `metrics.py` never touch `Session` directly. They call `queue_ops` functions, ensuring there's exactly one module responsible for each table's queries.

---

### 3. Validation & Models

**Modules:** [`validators.py`](queuectl/validators.py) · [`models.py`](queuectl/models.py) · [`exceptions.py`](queuectl/exceptions.py) · [`constants.py`](queuectl/constants.py)

| Module | Purpose |
|---|---|
| `validators.py` | Pure field validation functions — no DB access, reused by both `create_job` and `update_job` |
| `models.py` | SQLAlchemy declarative ORM classes: `Job`, `Config`, `Worker`, `JobLog` + `State` facade |
| `exceptions.py` | `QueueCTLError` base class + specific subclasses (`JobNotFoundError`, `DuplicateJobError`, etc.) |
| `constants.py` | Single source of truth for state names, default config values, heartbeat thresholds |

**Separation rationale:**
- Validators are DB-free → trivially unit-testable, reusable across create/update paths
- Duplicate-ID checking stays in `queue_ops.create_job` because it requires a DB read
- All exceptions subclass `QueueCTLError` so `cli.py` can `except QueueCTLError` once per command

---

### 4. Database Engine

**Module:** [`database.py`](queuectl/database.py)

Creates the SQLAlchemy engine with three critical SQLite PRAGMAs and the `BEGIN IMMEDIATE` event hooks:

```python
PRAGMA journal_mode=WAL      # Concurrent readers during writes
PRAGMA busy_timeout=30000    # Wait up to 30s for a locked DB
PRAGMA foreign_keys=ON       # Enforce FK constraints
```

**Session factory pattern:**
- `init_db()` — Creates tables + seeds default config. Safe to call repeatedly (idempotent).
- `get_session()` — Returns a configured session. Workers hold one session for their entire lifetime.

**DB path resolution:**
1. `QUEUECTL_DB` environment variable (if set)
2. `./queuectl_data/queuectl.db` (default, created on first use)

---

## Module Dependency Graph

```
                         cli.py
                        /  |   \
                       /   |    \
              queue_ops  config  worker_manager
              /  |   \     |         |
         dlq  metrics  validators   worker
              \   |   /     |         |
               models    constants  executor
                  |                   |
               database           (subprocess)
                  |
               SQLite
```

**Key constraints:**
- Arrows point downward only — no circular dependencies
- `worker.py` and `worker_manager.py` are the only modules that interact with OS processes
- `retry.py` is entirely pure (math only, no imports from the project)

---

## Module Reference

| Module | Lines | Responsibility |
|---|---|---|
| [`cli.py`](queuectl/cli.py) | ~600 | Click commands, Rich formatting, `QueueCTLError` → `ClickException` translation |
| [`queue_ops.py`](queuectl/queue_ops.py) | ~280 | All job CRUD + lifecycle state transitions (`claim`, `complete`, `fail`) |
| [`config.py`](queuectl/config.py) | ~150 | Config CRUD with per-key validators, `load_defaults` seeds on first use |
| [`dlq.py`](queuectl/dlq.py) | ~50 | DLQ operations — thin wrapper over `queue_ops.py` |
| [`metrics.py`](queuectl/metrics.py) | ~70 | Computes stats from `job_logs` — success rate, throughput, runtime stats |
| [`validators.py`](queuectl/validators.py) | ~50 | Pure field validation — no DB, reused by `create_job` and `update_job` |
| [`models.py`](queuectl/models.py) | ~100 | SQLAlchemy ORM: `Job`, `Config`, `Worker`, `JobLog`, `State` facade |
| [`database.py`](queuectl/database.py) | ~75 | Engine creation, `BEGIN IMMEDIATE` hooks, session factory |
| [`worker.py`](queuectl/worker.py) | ~115 | Single worker poll loop: claim → execute → complete/fail |
| [`worker_manager.py`](queuectl/worker_manager.py) | ~80 | Spawns/stops worker OS processes, heartbeat tracking |
| [`executor.py`](queuectl/executor.py) | ~100 | `run_command()` — subprocess execution with process-tree timeout kill |
| [`retry.py`](queuectl/retry.py) | ~20 | Pure functions: `calculate_delay()` and `is_dead()` |
| [`app_logging.py`](queuectl/app_logging.py) | ~80 | Three log files: `worker.log`, `queuectl.log`, `error.log` |
| [`exceptions.py`](queuectl/exceptions.py) | ~28 | `QueueCTLError` base + 5 specific exception types |
| [`constants.py`](queuectl/constants.py) | ~30 | State names, default config values, heartbeat thresholds |
| [`utils.py`](queuectl/utils.py) | ~30 | `utcnow()`, `after_seconds()`, `new_id()` |

---

## Worker Architecture

### Process Model

```
queuectl worker start --count 3
           │
           ▼
   worker_manager.start_workers()
           │
           ├── subprocess.Popen ──► worker.py:run()  [PID 41232]
           ├── subprocess.Popen ──► worker.py:run()  [PID 41233]
           └── subprocess.Popen ──► worker.py:run()  [PID 41234]
                                        │
                                   Each worker:
                                   • Own OS process (crash isolation)
                                   • Own SQLAlchemy session
                                   • Own connection to same SQLite file
                                   • Polls for jobs independently
```

Workers are **independent OS processes**, not threads. This provides:
- **Crash isolation** — one worker crashing doesn't affect others
- **No GIL contention** — true parallelism for subprocess management
- **Production-like deployment** — mirrors real worker infrastructure

### Worker Lifecycle

```
┌─────────┐     register in DB      ┌─────────┐
│  START   │ ───────────────────────► │ RUNNING │◄──────────────────┐
└─────────┘                          └────┬────┘                   │
                                          │                        │
                                    ┌─────▼──────┐                │
                                    │  Poll Loop  │                │
                                    └─────┬──────┘                │
                                          │                        │
                              ┌───────────┼───────────┐            │
                              │           │           │            │
                         No jobs     Job found   stop_requested    │
                              │           │           │            │
                         Heartbeat   ┌────▼────┐      │            │
                         + sleep     │  CLAIM  │      │            │
                              │      │  (BEGIN │      │            │
                              │      │IMMEDIATE)│     │            │
                              │      └────┬────┘      │            │
                              │           │           │            │
                              │      ┌────▼─────┐     │            │
                              │      │ EXECUTE  │     │            │
                              │      │(subprocess)    │            │
                              │      └────┬─────┘     │            │
                              │           │           │            │
                              │     Complete/Fail     │            │
                              │           │           │            │
                              └───────────┴───────┬───┘            │
                                                  │                │
                                          ┌───────▼──────┐         │
                                          │ Check stop?  │─── No ──┘
                                          └───────┬──────┘
                                                  │ Yes
                                          ┌───────▼──────┐
                                          │   STOPPED    │
                                          │ (update DB)  │
                                          └──────────────┘
```

**Graceful shutdown:** Workers check `stop_requested` _between_ jobs, never mid-execution. A running job always completes before the worker exits.

**Heartbeat:** Updated every poll cycle and after each job. Heartbeats older than `HEARTBEAT_STALE_SECONDS` (30s) are flagged as stale by `worker list`.

### Command Execution & Timeout Handling

```
executor.run_command(command, timeout_seconds)
           │
           ▼
   subprocess.Popen(command, shell=True)
           │
           │                    ┌──────────────┐
           │  ◄─── timeout ───►│ TimeoutExpired│
           │                    └──────┬───────┘
           │                           │
           ▼                           ▼
   Normal completion          _kill_process_tree()
   (capture stdout/stderr)         │
                              ┌────┴────────────────────┐
                              │ Windows                  │ POSIX
                              │ taskkill /F /T /PID      │ os.killpg(SIGKILL)
                              │ (recursive tree kill)    │ (process group kill)
                              └─────────────────────────┘
```

**Why process-tree kill?** With `shell=True`, the actual command runs as a grandchild process. Killing only the shell parent leaves the real command running as an orphan.

---

## Job Lifecycle State Machine

### State Transitions

```
                     enqueue
                        │
                        ▼
                   ┌─────────┐
            ┌──── │ PENDING  │ ◄──────────────────────┐
            │     └─────────┘                          │
            │  claim_job                               │
            │  (BEGIN IMMEDIATE)                       │ next_retry elapsed
            ▼                                          │
     ┌────────────┐                             ┌─────────┐
     │ PROCESSING │                             │ FAILED   │
     └────────────┘                             └─────────┘
            │                                          ▲
     executor.run_command                              │
            │                                          │
     ┌──────┴──────┐                                   │
     │             │                                   │
  exit = 0    exit ≠ 0 ──── retries remain ────────────┘
     │             │
     ▼             │ retries exhausted
┌───────────┐      │
│ COMPLETED │      ▼
└───────────┘   ┌──────┐
                │ DEAD │  ← This IS the DLQ
                └──────┘    (same table, state='dead')
                   │
                   │ dlq retry
                   │
                   ▼
              Back to PENDING (attempts reset to 0)
```

### Five States

| State | Meaning | Claimable? |
|---|---|---|
| `pending` | Awaiting first execution | ✅ Yes |
| `processing` | Currently being executed by a worker | ❌ No |
| `completed` | Finished successfully (exit code 0) | ❌ No |
| `failed` | Failed but retryable; `next_retry` set | ✅ When `next_retry ≤ now` |
| `dead` | Permanently failed (DLQ) | ❌ No (manual `dlq retry` only) |

### Claim Eligibility Rules

A job is claimable if:
```sql
(run_at IS NULL OR run_at <= now)
AND (
    state = 'pending'
    OR (state = 'failed' AND next_retry IS NOT NULL AND next_retry <= now)
)
ORDER BY priority DESC, created_at ASC
```

**Design choice:** Failed jobs stay `state='failed'` (not reset to `pending`) so `queuectl list --state failed` accurately counts retrying jobs.

---

## Concurrency & Locking Model

### BEGIN IMMEDIATE Semantics

The central concurrency guarantee: **no two workers can ever execute the same job**.

```
Worker A                              Worker B
────────                              ────────
BEGIN IMMEDIATE  (acquires write lock)
SELECT job X (state='pending')
UPDATE job X → 'processing'
COMMIT (releases lock)
                                      BEGIN IMMEDIATE (was blocked, now gets lock)
                                      SELECT ... (job X is 'processing', skipped)
                                      SELECT job Y instead
                                      UPDATE job Y → 'processing'
                                      COMMIT
```

### SQLAlchemy Integration

pysqlite (Python's default SQLite driver) manages its own implicit transactions, which prevents using `BEGIN IMMEDIATE`. Two engine event hooks restore the guarantee:

```python
@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    dbapi_connection.isolation_level = None   # Disable pysqlite's transactions

@event.listens_for(engine, "begin")
def _do_begin_immediate(conn):
    conn.exec_driver_sql("BEGIN IMMEDIATE")   # Use SQLite's native locking
```

This applies to **every** transaction on the engine — reads included.

### Polling Loop Safety Rule

> ⚠️ **Any loop that reads from the database must `session.commit()` on every iteration.**

Because every transaction opens with `BEGIN IMMEDIATE`, a polling loop that reads without committing holds the write lock for its entire duration, starving all worker processes. This pattern caused two separate bugs:

1. **`worker stop`** — the stop-polling loop held the lock, preventing workers from committing their "stopped" status
2. **`benchmark`** — the completion-polling loop held the lock, preventing workers from claiming jobs

---

## Database Schema

### Entity-Relationship Diagram

```
┌───────────────────────────────┐           ┌──────────────────────────┐
│            jobs               │           │        job_logs          │
├───────────────────────────────┤           ├──────────────────────────┤
│ id              TEXT    PK    │◄── soft ──┤ job_id         TEXT      │
│ command         TEXT          │           │ id             INT   PK  │
│ state           TEXT          │           │ attempt        INT       │
│ attempts        INT           │           │ stdout         TEXT      │
│ max_retries     INT           │           │ stderr         TEXT      │
│ priority        INT           │           │ exit_code      INT       │
│ run_at          DATETIME      │           │ started_at     DATETIME  │
│ next_retry      DATETIME      │           │ finished_at    DATETIME  │
│ timeout_secs    INT           │           └──────────────────────────┘
│ worker_id       TEXT          │
│ last_error      TEXT          │
│ created_at      DATETIME      │
│ updated_at      DATETIME      │
└───────────────────────────────┘

┌───────────────────────────────┐           ┌──────────────────────────┐
│           workers             │           │         config           │
├───────────────────────────────┤           ├──────────────────────────┤
│ worker_id       TEXT    PK    │           │ key            TEXT  PK  │
│ pid             INT           │           │ value          TEXT      │
│ status          TEXT          │           └──────────────────────────┘
│ stop_requested  BOOL          │
│ current_job_id  TEXT          │
│ started_at      DATETIME      │
│ last_heartbeat  DATETIME      │
└───────────────────────────────┘
```

### Table Details

| Table | Rows represent | Growth pattern |
|---|---|---|
| `jobs` | One row per enqueued job | Grows with enqueue, deleted manually |
| `job_logs` | One row per execution attempt | Append-only (survives job deletion) |
| `workers` | One row per worker process ever started | Grows with worker starts |
| `config` | Key-value tunables | Fixed set of ~7 keys, seeded on first use |

### Soft References (No Foreign Keys)

`jobs.worker_id → workers` and `job_logs.job_id → jobs` are **intentionally soft references**:

- **Job logs survive job deletion** — audit trail for post-mortem analysis
- **Worker rows can outlive jobs** — a stopped worker's history remains for inspection
- Simplifies cleanup: deleting a job doesn't cascade to logs

---

## Error Handling Strategy

```
exceptions.py hierarchy:

QueueCTLError (base)
├── JobNotFoundError       — job ID doesn't exist
├── DuplicateJobError      — enqueuing with an existing ID
├── InvalidJobDataError    — field validation failure (from validators.py)
├── InvalidJobStateError   — wrong state for operation (e.g., DLQ retry on non-dead job)
├── InvalidConfiguration   — unknown config key or invalid value
└── DatabaseError          — unexpected SQLAlchemy failure
```

**Translation in cli.py:**
```python
try:
    result = queue_ops.some_operation(session, ...)
    output = format_result(result)  # Build output while session is open
except QueueCTLError as exc:
    raise click.ClickException(str(exc))
finally:
    session.close()
click.echo(output)  # Print after session is closed
```

---

## Retry & Backoff Model

Implemented in [`retry.py`](queuectl/retry.py) as pure math functions (no DB dependency):

```
Delay = backoff_base ^ attempts

With backoff_base = 2:
  Attempt 1 → 2s delay
  Attempt 2 → 4s delay
  Attempt 3 → 8s delay
  Attempt 4 → 16s delay

Dead when: attempts >= max_retries
```

**How it's applied:**
1. `worker.py` calls `executor.run_command()` — gets a non-zero exit code
2. `queue_ops.fail_job()` increments `attempts`, calls `retry.is_dead()`
3. If not dead: `state='failed'`, `next_retry = now + calculate_delay(attempts, base)`
4. If dead: `state='dead'`, `next_retry = None` — job enters the DLQ

**Live reload:** `backoff_base` is read from the config table each worker cycle, so changes via `queuectl config set backoff_base 3` take effect immediately on running workers.

---

## Configuration Architecture

Config is stored in the `config` table as key-value pairs with per-key validation:

| Key | Default | Validated | Live-reloaded by workers? |
|---|---|---|---|
| `max_retries` | `3` | `≥ 0` | ❌ Captured per-job at enqueue |
| `backoff_base` | `2` | `> 1` | ✅ Read each cycle |
| `poll_interval` | `1` | `> 0` | ✅ Read each cycle |
| `heartbeat_interval` | `2` | `> 0` | ✅ |
| `timeout` | `30` | `> 0` | ❌ Per-job at enqueue |
| `default_priority` | `0` | `≥ 0` | ❌ Per-job at enqueue |
| `max_workers` | `10` | `> 0` | ❌ Checked at `worker start` |

**Seeding:** `config.load_defaults()` runs on every `get_session()` call. It only inserts keys that don't already exist, making it safe to call repeatedly.

---

## Logging Architecture

Three operational log files (directory controlled by `QUEUECTL_LOG_DIR`, default `./logs`):

```
┌──────────────┐     Python logging       ┌──────────────┐
│  worker.py   │ ───── INFO/WARNING ─────► │ worker.log   │
│              │ ───── ERROR ────────┐     └──────────────┘
└──────────────┘                    │
                                    ├────► ┌──────────────┐
┌──────────────┐                    │     │  error.log   │
│   cli.py     │ ───── ERROR ───────┘     │ (all ERROR)  │
│              │ ───── INFO ─────────────► └──────────────┘
└──────────────┘                          ┌──────────────┐
                                          │ queuectl.log │
                                          └──────────────┘
```

**Separate from `job_logs` table:** Operational logs (startup, shutdown, config changes) go to files. Structured per-attempt data (stdout, stderr, exit code, duration) goes to the `job_logs` database table for querying via `queuectl stats`.

---

## Testing Architecture

### Test Pyramid

```
┌─────────────────────────────────────┐
│     End-to-End (validate_e2e.py)    │  Real CLI + real worker processes
│        7 scenarios                  │  Tests: completion, retry→DLQ,
│                                     │  parallel workers, persistence,
│                                     │  graceful shutdown, crash isolation
├─────────────────────────────────────┤
│    Integration (validate_db.py)     │  Direct queue_ops/config calls
│    DB CRUD + restart persistence    │  against a real SQLite file
├─────────────────────────────────────┤
│       Unit Tests (122 tests)        │  One test file per module
│     pytest · mostly mock-free       │  Real SQLite, real threads,
│                                     │  real subprocesses
└─────────────────────────────────────┘
```

### Test Coverage by Module

| Test File | Covers | Notable |
|---|---|---|
| `test_queue_ops.py` | CRUD, state transitions | **8-thread concurrent claim race** — no duplicates |
| `test_executor.py` | Command execution | **Real subprocess timeout with process-tree kill** |
| `test_worker.py` | Worker poll loop | Background thread with real session |
| `test_cli.py` | Full CLI via `CliRunner` | Includes benchmark regression test |
| `test_retry.py` | Backoff math | Pure function tests, no fixtures |
| `test_dlq.py` | DLQ list/retry/delete | State guard assertions |
| `test_config.py` | Config CRUD | Validation, export/import |
| `test_database.py` | Engine, sessions | Session lifecycle |
| `test_metrics.py` | Stats computation | Aggregation correctness |
| `test_app_logging.py` | Log files | File creation, error propagation |
| `test_worker_manager.py` | Worker spawn/stop | Process management |

### CI Pipeline

```
GitHub Actions (.github/workflows/ci.yml)
  Matrix: Ubuntu + Windows × Python 3.x
  │
  ├── black --check          (formatting)
  ├── isort --check-only     (import order)
  ├── pytest                 (122 unit tests)
  ├── validate_db.py         (integration)
  └── validate_e2e.py        (end-to-end)
```

---

## Cross-Platform Considerations

| Area | Windows | POSIX |
|---|---|---|
| Worker spawning | `CREATE_NEW_PROCESS_GROUP \| DETACHED_PROCESS` | `start_new_session=True` |
| Timeout kill | `taskkill /F /T /PID` (recursive tree kill) | `os.killpg(SIGKILL)` (process group) |
| Shell execution | `cmd.exe /c <command>` | `/bin/sh -c <command>` |
| Worker CWD | Explicit `cwd=os.getcwd()` (required — detached processes inherit wrong CWD) | Inherited naturally |
| Stop mechanism | DB flag polling (no `SIGTERM` support) | DB flag polling (consistent) |

---

## Directory Structure

```
QueueCTL/
├── queuectl/                    # Main package
│   ├── __init__.py
│   ├── cli.py                   # CLI entry point (Click + Rich)
│   ├── queue_ops.py             # Job CRUD + lifecycle transitions
│   ├── config.py                # Config CRUD + validation
│   ├── dlq.py                   # Dead Letter Queue operations
│   ├── metrics.py               # Execution statistics
│   ├── validators.py            # Pure field validation
│   ├── models.py                # SQLAlchemy ORM models
│   ├── database.py              # Engine + session factory
│   ├── worker.py                # Worker poll loop
│   ├── worker_manager.py        # Worker process management
│   ├── executor.py              # Command execution + timeout
│   ├── retry.py                 # Backoff math
│   ├── app_logging.py           # Logging setup
│   ├── exceptions.py            # Exception hierarchy
│   ├── constants.py             # State names + defaults
│   └── utils.py                 # Utility functions
├── tests/                       # Test suite (122 tests)
│   ├── conftest.py              # Shared fixtures
│   ├── test_queue_ops.py
│   ├── test_cli.py
│   ├── test_executor.py
│   ├── test_worker.py
│   ├── test_config.py
│   ├── test_dlq.py
│   ├── test_metrics.py
│   ├── test_retry.py
│   ├── test_database.py
│   ├── test_app_logging.py
│   └── test_worker_manager.py
├── scripts/                     # Validation & demo scripts
│   ├── validate_e2e.py          # End-to-end test (7 scenarios)
│   ├── validate_db.py           # Integration test
│   └── worker_demo.py           # Live demo script
├── .github/workflows/ci.yml    # CI pipeline
├── Dockerfile                   # Container support
├── pyproject.toml               # Project config + entry point
├── requirements.txt             # Pinned dependencies
├── README.md                    # User-facing documentation
├── ARCHITECTURE.md              # This file
└── design.md                    # Decision log & bug post-mortems
```

---

## Sequence Diagram: Job from Enqueue to Completion

```
User          CLI (cli.py)      queue_ops.py         SQLite          worker.py
 │                │                   │                 │                │
 │  enqueue "..." │                   │                 │                │
 │───────────────►│                   │                 │                │
 │                │ create_job(data)  │                 │                │
 │                │──────────────────►│                 │                │
 │                │                   │ INSERT jobs     │                │
 │                │                   │────────────────►│                │
 │                │                   │◄────────────────│                │
 │                │◄──────────────────│                 │                │
 │◄───────────────│  "Job Created"    │                 │                │
 │                │                   │                 │  (polling)     │
 │                │                   │                 │◄───────────────│
 │                │                   │                 │ BEGIN IMMEDIATE│
 │                │                   │                 │ SELECT pending │
 │                │                   │                 │ UPDATE proc.   │
 │                │                   │                 │ COMMIT         │
 │                │                   │                 │───────────────►│
 │                │                   │                 │  run_command() │
 │                │                   │                 │◄───────────────│
 │                │                   │                 │ complete_job   │
 │                │                   │                 │  (COMMIT)      │
 │                │                   │                 │                │
 │  status        │                   │                 │                │
 │───────────────►│ status_summary()  │                 │                │
 │                │──────────────────►│ SELECT counts   │                │
 │                │                   │────────────────►│                │
 │◄───────────────│◄──────────────────│◄────────────────│                │
```

---

<p align="center">
  <em>For the "why" behind specific decisions and bug post-mortems, see <a href="design.md">design.md</a>.</em><br>
  <em>For CLI usage and command reference, see <a href="README.md">README.md</a>.</em>
</p>
