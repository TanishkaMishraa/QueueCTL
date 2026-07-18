<p align="center">
  <h1 align="center">⚡ QueueCTL</h1>
  <p align="center">
    <strong>A powerful CLI-based background job queue system built in Python</strong>
  </p>
  <p align="center">
    <a href="#-quick-start"><img src="https://img.shields.io/badge/python-3.9%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.9+"></a>
    <a href="#-architecture"><img src="https://img.shields.io/badge/database-SQLite-003B57?style=for-the-badge&logo=sqlite&logoColor=white" alt="SQLite"></a>
    <a href="#-testing"><img src="https://img.shields.io/badge/tests-122%20passing-brightgreen?style=for-the-badge" alt="Tests"></a>
    <a href="#-license"><img src="https://img.shields.io/badge/license-MIT-blue?style=for-the-badge" alt="License"></a>
  </p>
</p>

<br>

> Enqueue jobs · Run parallel workers · Retry with exponential backoff · Dead Letter Queue  
> All state persisted in SQLite — everything survives a restart.

---

## 📑 Table of Contents

- [✨ Features](#-features)
- [🚀 Quick Start](#-quick-start)
- [📖 Usage Examples](#-usage-examples)
- [📋 Command Reference](#-command-reference)
- [🏗️ Architecture](#️-architecture)
- [🔄 Job Lifecycle](#-job-lifecycle)
- [🗄️ Database Schema](#️-database-schema)
- [⚙️ Configuration](#️-configuration)
- [🧪 Testing](#-testing)
- [🐳 Docker](#-docker)
- [🏆 Bonus Features](#-bonus-features)
- [📐 Design Decisions & Trade-offs](#-design-decisions--trade-offs)
- [🔮 Future Improvements](#-future-improvements)
- [🎬 Demo](#-demo)

---

## ✨ Features

<table>
<tr>
<td width="50%">

### Core

- 🔁 **Retry with Exponential Backoff** — configurable `backoff_base^attempts` delay
- 💀 **Dead Letter Queue (DLQ)** — permanently failed jobs quarantined for review
- 👷 **Parallel Workers** — independent OS processes with crash isolation
- 💾 **Persistent State** — SQLite with WAL mode, survives any restart
- 🔒 **Atomic Job Claiming** — `BEGIN IMMEDIATE` prevents duplicate execution

</td>
<td width="50%">

### Bonus

- ⏱️ **Job Timeouts** — per-job time limits with full process-tree kill
- 📊 **Priority Queues** — higher priority jobs claimed first
- 🕐 **Scheduled Jobs** — `run_at` delays execution until a target time
- 📈 **Execution Stats & Metrics** — success rate, throughput, runtime stats
- 🩺 **Health Checks** — database, workers, queue, config diagnostics
- 🖥️ **Rich Dashboard** — multi-panel terminal UI via Rich

</td>
</tr>
</table>

---

## 🚀 Quick Start

### Prerequisites

- **Python 3.9+**

### Installation

```bash
git clone <your-fork-url>
cd QueueCTL
python -m venv .venv

# Activate the virtual environment
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"
```

This installs the `queuectl` CLI command (via `pyproject.toml` entry point) powered by:

| Dependency     | Purpose                              |
| -------------- | ------------------------------------ |
| **Click**      | CLI framework                        |
| **Rich**       | Formatted panels, tables & dashboard |
| **SQLAlchemy** | ORM over SQLite                      |
| **pytest**     | Testing (dev dependency)             |

### Database Location

By default, state lives in `./queuectl_data/queuectl.db` (created on first use).  
Override with the **`QUEUECTL_DB`** environment variable.

---

## 📖 Usage Examples

### Enqueue Jobs

```bash
# From a plain shell command
$ queuectl enqueue "echo Hello World" --priority high --max-retries 5
┌── Job Created ──────┐
│ ID: 6362703482c3     │
│ State: pending       │
│ Max Retries: 5       │
│ Priority: 10         │
└──────────────────────┘

# From JSON (auto-detected if argument starts with '{')
$ queuectl enqueue '{"id":"job2","command":"exit 1","max_retries":2}'
┌── Job Created ──┐
│ ID: job2         │
│ State: pending   │
│ Max Retries: 2   │
└──────────────────┘
```

### Start & Manage Workers

```bash
# Start 3 background workers
$ queuectl worker start --count 3
Started 3 worker(s): 3f9a1c2b4d5e, 7b2e8f1a9c3d, 0d4f6a2e8b1c

# List workers (stale heartbeats flagged automatically)
$ queuectl worker list
Workers Running: 3
  3f9a1c2b4d5e   pid=41232   status=running   current_job=-   heartbeat=3s ago
  7b2e8f1a9c3d   pid=41233   status=running   current_job=-   heartbeat=3s ago
  0d4f6a2e8b1c   pid=41234   status=running   current_job=-   heartbeat=47s ago  [STALE - no heartbeat, likely crashed]

# Graceful shutdown
$ queuectl worker stop
Stop requested for 3 worker(s); 3 confirmed stopped.
```

### Monitor & Inspect

```bash
# Quick status overview
$ queuectl status
Total jobs: 2
  pending    0
  processing 0
  completed  1
  failed     0
  dead       1

Attempts logged: 3  Success rate: 33.3%

# Detailed execution statistics
$ queuectl stats
Jobs Executed
  Completed : 95
  Failed    : 8
  Retries   : 12
  DLQ       : 4

Average Runtime : 1.84 sec
Success Rate: 92.2%
Throughput  : 47.5 jobs/hour

# Health check
$ queuectl health
Database       [OK] Healthy
Workers        [OK] Running (3 active)
Queue          [OK] Accepting Jobs
DLQ            4 Job(s)
Configuration  [OK] Loaded
```

### Job Operations

```bash
# List jobs filtered by state
$ queuectl list --state completed

# Inspect a specific job
$ queuectl job show job2
┌──────────── Job job2 ────────────┐
│ ID: job2                         │
│ Command: exit 1                  │
│ State: dead                      │
│ Attempts: 2/2                    │
│ Last Error: exit code 1          │
└──────────────────────────────────┘

# Delete a job
$ queuectl job delete job2
```

### Dead Letter Queue

```bash
$ queuectl dlq list          # List dead jobs
$ queuectl dlq count         # Count dead jobs
$ queuectl dlq retry <id>    # Requeue a dead job back to pending
$ queuectl dlq delete <id>   # Permanently remove from DLQ
```

### Configuration

```bash
$ queuectl config show       # View all settings
$ queuectl config set max-retries 5
$ queuectl config export backup.json
$ queuectl config import backup.json
```

> **💡 Note:** Panels and tables above are Rich output — actual box-drawing characters vary by terminal.

> **🪟 Windows Note:** Job commands run through `cmd.exe` via `shell=True`. Use `python -c "import time; time.sleep(2)"` instead of `sleep 2` for cross-platform compatibility.

---

## 📋 Command Reference

### Jobs

| Command                                     | Description                                                                                                       |
| ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `queuectl enqueue "<command>" [options]`    | Add a job. Options: `--id`, `--priority` (int or `low`/`normal`/`high`), `--timeout`, `--max-retries`, `--run-at` |
| `queuectl enqueue '<json>'`                 | Add a job via JSON (auto-detected if starts with `{`)                                                             |
| `queuectl list [--state STATE] [--limit N]` | List jobs, optionally filtered by state                                                                           |
| `queuectl job show <id>`                    | Full details for one job                                                                                          |
| `queuectl job delete <id> [--yes]`          | Delete a job permanently                                                                                          |

### Workers

| Command                                          | Description                                          |
| ------------------------------------------------ | ---------------------------------------------------- |
| `queuectl worker start --count N [--foreground]` | Start N worker processes (background by default)     |
| `queuectl worker stop [--timeout SEC]`           | Graceful shutdown — workers finish current job first |
| `queuectl worker list`                           | List workers with PID, status, heartbeat age         |

### Dead Letter Queue

| Command                            | Description                                    |
| ---------------------------------- | ---------------------------------------------- |
| `queuectl dlq list`                | List all dead jobs                             |
| `queuectl dlq count`               | Count dead jobs                                |
| `queuectl dlq retry <id>`          | Requeue a dead job back to `pending`           |
| `queuectl dlq delete <id> [--yes]` | Permanently delete (only works on `dead` jobs) |

### Monitoring

| Command              | Description                                        |
| -------------------- | -------------------------------------------------- |
| `queuectl status`    | Job counts by state, attempt stats, active workers |
| `queuectl stats`     | Execution metrics: rates, runtimes, throughput     |
| `queuectl health`    | System diagnostics: DB, workers, queue, config     |
| `queuectl dashboard` | Rich multi-panel terminal UI                       |

### Configuration

| Command                             | Description                                         |
| ----------------------------------- | --------------------------------------------------- |
| `queuectl config show`              | Display all config values (alias: `list`)           |
| `queuectl config set <key> <value>` | Update a config key (validated before write)        |
| `queuectl config get <key>`         | Read a single config value                          |
| `queuectl config delete <key>`      | Reset a key to its default                          |
| `queuectl config reset [key]`       | Reset one key or all keys to defaults               |
| `queuectl config export <file>`     | Export config to JSON                               |
| `queuectl config import <file>`     | Import config from JSON (validated before applying) |

### Benchmark

| Command                                   | Description                                                        |
| ----------------------------------------- | ------------------------------------------------------------------ |
| `queuectl benchmark --jobs N --workers W` | Performance test: enqueue N jobs, run W workers, report throughput |

> All commands support `--help` for detailed usage.

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────┐
│                     CLI Layer (cli.py)                    │
│          Click commands · Rich output · Error handling    │
└──────────────────┬───────────────────────────────────────┘
                   │
┌──────────────────▼───────────────────────────────────────┐
│              Repository Layer                            │
│  queue_ops.py · config.py · dlq.py · metrics.py          │
│  ─── the ONLY modules that write ORM queries ───         │
└──────────────────┬───────────────────────────────────────┘
                   │
┌──────────────────▼───────────────────────────────────────┐
│           Validation & Models                            │
│  validators.py (pure field checks, no DB access)         │
│  models.py (declarative ORM: Job, Config, Worker, Log)   │
│  exceptions.py (QueueCTLError hierarchy)                 │
│  constants.py (state names, defaults)                    │
└──────────────────┬───────────────────────────────────────┘
                   │
┌──────────────────▼───────────────────────────────────────┐
│              Database Engine (database.py)                │
│       SQLAlchemy · BEGIN IMMEDIATE · WAL mode             │
└──────────────────┬───────────────────────────────────────┘
                   │
              ┌────▼────┐
              │  SQLite  │
              └─────────┘
```

### Module Map

| Module              | Responsibility                                                                 |
| ------------------- | ------------------------------------------------------------------------------ |
| `cli.py`            | Click commands, Rich formatting, translates `QueueCTLError` → `ClickException` |
| `queue_ops.py`      | All job CRUD + lifecycle state transitions (`claim`, `complete`, `fail`)       |
| `config.py`         | Config CRUD with per-key validators, `load_defaults` seeds on first use        |
| `dlq.py`            | DLQ operations — delegates to `queue_ops.py`, never writes its own queries     |
| `metrics.py`        | Computes stats from `job_logs` — delegates to `queue_ops.py`                   |
| `validators.py`     | Pure field validation — no DB access, reused by `create_job` and `update_job`  |
| `models.py`         | SQLAlchemy ORM classes: `Job`, `Config`, `Worker`, `JobLog`                    |
| `database.py`       | Engine creation, `BEGIN IMMEDIATE` hooks, session factory                      |
| `worker.py`         | Single worker poll loop: claim → execute → complete/fail                       |
| `worker_manager.py` | Spawns/stops worker OS processes, heartbeat tracking                           |
| `executor.py`       | `run_command()` — subprocess execution with process-tree timeout kill          |
| `retry.py`          | Pure functions: `calculate_delay()` and `is_dead()`                            |
| `app_logging.py`    | Three log files: `worker.log`, `queuectl.log`, `error.log`                     |
| `exceptions.py`     | `QueueCTLError` base + `JobNotFoundError`, `DuplicateJobError`, etc.           |
| `constants.py`      | State names, default config values, heartbeat thresholds                       |

---

## 🔄 Job Lifecycle

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
     executor.run_command                             │
           │                                          │
     ┌─────┴──────┐                                   │
     │            │                                   │
 exit = 0    exit ≠ 0 ──── retries remain ────────────┘
     │            │
     ▼            │ retries exhausted
┌───────────┐     │
│ COMPLETED │     ▼
└───────────┘  ┌──────┐
               │ DEAD │  ← This IS the DLQ
               └──────┘    (same table, state='dead')
```

**Key points:**

- `failed` stays visible as a distinct state (not reset to `pending`) so `queuectl list --state failed` accurately shows retrying jobs
- `dead` = DLQ — no separate table, just a `state='dead'` filter
- `claim_job` treats both `pending` and `failed` (with elapsed `next_retry`) as claimable

---

## 🗄️ Database Schema

Four SQLAlchemy ORM tables in a single SQLite file:

```
┌───────────────────────────┐        ┌──────────────────────┐
│ jobs                      │        │ job_logs             │
├───────────────────────────┤        ├──────────────────────┤
│ id            TEXT PK     │◄──┐    │ id           PK      │
│ command       TEXT        │   │    │ job_id       TEXT ───┼──┐
│ state         TEXT        │   └────┤                      │  │
│ attempts      INT         │        │ attempt      INT     │  │
│ max_retries   INT         │        │ stdout       TEXT    │  │
│ priority      INT         │        │ stderr       TEXT    │  │
│ run_at        DATETIME    │        │ exit_code    INT     │  │
│ next_retry    DATETIME    │        │ started_at   DATETIME│  │
│ timeout_secs  INT         │        │ finished_at  DATETIME│  │
│ worker_id     TEXT        │        └──────────────────────┘  │
│ last_error    TEXT        │   (soft reference — logs outlive │
│ created_at    DATETIME    │    a deleted job for audit)──────┘
│ updated_at    DATETIME    │
└───────────────────────────┘

┌───────────────────────────┐        ┌──────────────────────┐
│ workers                   │        │ config               │
├───────────────────────────┤        ├──────────────────────┤
│ worker_id     TEXT PK     │        │ key         TEXT PK  │
│ pid           INT         │        │ value       TEXT     │
│ status        TEXT        │        └──────────────────────┘
│ stop_requested BOOL       │
│ current_job_id TEXT       │
│ started_at    DATETIME    │
│ last_heartbeat DATETIME   │
└───────────────────────────┘
```

> **No foreign keys** — `jobs.worker_id → workers` and `job_logs.job_id → jobs` are intentionally soft references. Job logs survive job deletion (audit trail), and worker rows can outlive the jobs they processed.

---

## ⚙️ Configuration

All tunables managed via `queuectl config`:

| Key                  | Default | Description                                                       | Validation |
| -------------------- | ------- | ----------------------------------------------------------------- | ---------- |
| `max_retries`        | `3`     | Default retry limit for new jobs                                  | `≥ 0`      |
| `backoff_base`       | `2`     | Exponential backoff base (`delay = base^attempts`)                | `> 1`      |
| `poll_interval`      | `1`     | Seconds between worker poll cycles                                | `> 0`      |
| `heartbeat_interval` | `2`     | Seconds between worker heartbeat updates                          | `> 0`      |
| `timeout`            | `30`    | Default timeout (seconds); only applied if job explicitly sets it | `> 0`      |
| `default_priority`   | `0`     | Default priority for new jobs                                     | `≥ 0`      |
| `max_workers`        | `10`    | Maximum concurrent workers                                        | `> 0`      |

**Live reload:** `poll_interval` and `backoff_base` are read fresh each worker cycle — changes take effect on running workers immediately. `max_retries` and `priority` are captured per-job at enqueue time.

---

## 🧪 Testing

### Unit Tests — 122 tests

```bash
pytest
```

One test file per module, mostly mock-free — real SQLite files, real threads for concurrency, real subprocesses for timeout tests.

| Test File                | Covers                                                      |
| ------------------------ | ----------------------------------------------------------- |
| `test_database.py`       | Engine creation, session lifecycle                          |
| `test_queue_ops.py`      | CRUD, state transitions, **8-thread concurrent claim race** |
| `test_executor.py`       | Command execution, **timeout with process-tree kill**       |
| `test_retry.py`          | Backoff delay calculation, dead/alive boundary              |
| `test_dlq.py`            | DLQ list, retry, delete, state guard                        |
| `test_worker.py`         | Worker poll loop (background thread)                        |
| `test_config.py`         | Config CRUD, validation, export/import                      |
| `test_metrics.py`        | Stats computation                                           |
| `test_app_logging.py`    | Log file creation, error propagation                        |
| `test_worker_manager.py` | Worker spawn/stop                                           |
| `test_cli.py`            | Full CLI via `CliRunner`, including benchmark               |

### End-to-End Validation

```bash
python scripts/validate_e2e.py
```

Spawns the real CLI and real worker processes. Covers **7 scenarios**:

1. ✅ Basic job completion
2. 🔁 Retry → backoff → DLQ
3. 👷 Parallel workers, no duplicate execution (4 workers × 20 jobs)
4. ❌ Invalid commands fail gracefully
5. 💾 Persistence across restart
6. 🛑 Graceful shutdown waits for in-flight job
7. 💥 Force-killing one worker doesn't affect others

### Integration Check

```bash
python scripts/validate_db.py
```

Direct `queue_ops.py`/`config.py` calls against a real SQLite file: Create → Read → Update → Delete, duplicate/missing-id errors, config persistence across restart.

### Live Demo Script

```bash
python scripts/worker_demo.py
```

Enqueues a varied batch (success, failure→retry→DLQ, priority, invalid command), starts 2 workers, and prints `status` every 2 seconds for 10 seconds.

### Performance Benchmark

```bash
queuectl benchmark --jobs 100 --workers 4
```

### Continuous Integration

`.github/workflows/ci.yml` runs on every push/PR to `main`:

| Step           | Tool                      |
| -------------- | ------------------------- |
| Formatting     | `black --check`           |
| Import order   | `isort --check-only`      |
| Unit tests     | `pytest`                  |
| DB integration | `scripts/validate_db.py`  |
| End-to-end     | `scripts/validate_e2e.py` |

Runs on a **matrix of Ubuntu + Windows** across two Python versions, exercising both Windows-specific (`taskkill`, `cwd`-pinning) and POSIX (`os.killpg`, `SIGKILL`) code paths.

---

## 🐳 Docker

```bash
# Build
docker build -t queuectl .

# Run a one-shot command
docker run --rm -v queuectl-data:/data \
  -e QUEUECTL_DB=/data/queuectl.db \
  queuectl enqueue "echo hi"

# Run a persistent worker (--foreground required in containers)
docker run -d --name queuectl-worker \
  -v queuectl-data:/data \
  -e QUEUECTL_DB=/data/queuectl.db \
  queuectl worker start --count 1 --foreground
```

---

## 🏆 Bonus Features

| Feature                   | Implementation                                                                           |
| ------------------------- | ---------------------------------------------------------------------------------------- |
| ⏱️ Job timeout handling   | `timeout_seconds` per job; full process-tree kill on expiry                              |
| 📊 Priority queues        | `priority` field, `ORDER BY` in claim query; `--priority low/normal/high`                |
| 🕐 Scheduled/delayed jobs | `run_at` ISO timestamp; extra `WHERE` clause in claim query                              |
| 📝 Job output logging     | `job_logs` table: stdout, stderr, exit code, duration per attempt                        |
| 📋 Operational logging    | `worker.log`, `queuectl.log`, `error.log` (auto-propagated via Python logging hierarchy) |
| 📈 Execution stats        | `queuectl stats`: success/failure/retry rate, avg/min/max runtime, throughput            |
| 🩺 Health check           | `queuectl health`: DB, workers, queue, DLQ, config diagnostics                           |
| 🖥️ Rich dashboard         | `queuectl dashboard`: multi-panel terminal UI                                            |
| ⚙️ Config import/export   | `queuectl config export/import <file>`; per-key validation, atomic import                |
| 🏎️ Benchmark              | `queuectl benchmark --jobs N --workers W`                                                |
| 🐳 Docker                 | `Dockerfile` with volume mount for persistent data                                       |
| 🔄 CI/CD                  | GitHub Actions: lint + test on Ubuntu & Windows matrix                                   |

---

## 📐 Design Decisions & Trade-offs

<details>
<summary><b>🔒 SQLite over JSON files</b></summary>

SQLite provides real transactional locking (`BEGIN IMMEDIATE`) out of the box — exactly what's needed to prevent duplicate job claims across worker processes. A hand-rolled JSON + file lock would reimplement the same guarantee less reliably.

</details>

<details>
<summary><b>🗃️ SQLAlchemy ORM over raw sqlite3</b></summary>

Models are declarative Python classes instead of hand-written SQL. The one cost: SQLAlchemy's default SQLite driver doesn't naturally support `BEGIN IMMEDIATE`, so `database.py` disables pysqlite's implicit transactions and re-emits it manually via engine events.

</details>

<details>
<summary><b>👷 OS processes, not threads</b></summary>

Workers are separate OS processes — mirrors real deployment, provides crash isolation, sidesteps the GIL. Worker liveness is tracked via DB row + heartbeat instead of in-memory handles.

</details>

<details>
<summary><b>🛑 Cooperative stop via DB flag, not OS signals</b></summary>

Chosen for cross-platform consistency (developed/tested on Windows). Workers check `stop_requested` between jobs — a running job is never interrupted. Trade-off: up to one `poll_interval` delay before a stop is noticed.

</details>

<details>
<summary><b>💀 DLQ is a state, not a separate store</b></summary>

`dead` is just another job state — DLQ operations reuse the same schema and code paths instead of duplicating them.

</details>

<details>
<summary><b>🐚 shell=True for job commands</b></summary>

Matches the assignment's `sleep 2` / `echo hello` examples. Commands are trusted input — same assumption as any cron-like job runner.

</details>

<details>
<summary><b>⏱️ Timeout kills the process tree, not just the shell</b></summary>

With `shell=True`, the real command runs as a grandchild process. `executor.py` uses `Popen` + manual `TimeoutExpired` handler that kills the entire tree (`taskkill /F /T` on Windows, `os.killpg` on POSIX) instead of just the direct child.

</details>

<details>
<summary><b>🔄 Failed jobs stay `failed`, not reset to `pending`</b></summary>

A retry-scheduled job keeps `state='failed'` until its backoff elapses. This preserves observability — `queuectl list --state failed` accurately counts retrying jobs. The claim query treats both `pending` and eligible `failed` jobs as claimable.

</details>

---

## 📝 Logging

Three operational log files (override directory with `QUEUECTL_LOG_DIR`, default `./logs`):

| Log File       | Contents                                                                  |
| -------------- | ------------------------------------------------------------------------- |
| `worker.log`   | Worker lifecycle: started, picked up job, completed/failed/DLQ, stopped   |
| `queuectl.log` | CLI events: job created/deleted, config changed, DLQ manual retry         |
| `error.log`    | All `ERROR`-level records (auto-collected via Python logging propagation) |

> These are separate from the `job_logs` DB table, which stores structured per-attempt stdout/stderr/exit-code data.

---

## 🔮 Future Improvements

| Area                         | Description                                                                                                                                                                          |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Redis/PostgreSQL backend** | SQLite's single-writer model tops out before high-throughput needs. Redis (`RPOPLPUSH`) or PostgreSQL (`SELECT ... FOR UPDATE SKIP LOCKED`) would remove the single-machine ceiling. |
| **REST API**                 | A FastAPI/Flask layer over `queue_ops.py`/`dlq.py` would expose operations to non-CLI clients without duplicating business logic.                                                    |
| **Distributed workers**      | Workers are already crash-isolated OS processes — swapping the backend enables Kubernetes deployments with `HorizontalPodAutoscaler` on queue depth.                                 |
| **Web dashboard**            | `queuectl dashboard` already renders from the same data functions a web UI would use — a Flask + HTMX frontend could reuse that data layer as-is.                                    |

---

## 🎬 Demo

[▶️ Watch the project demo video](https://drive.google.com/drive/folders/171bTidnP3uwwvDEmn7IiJq5qlQ5qIJHj?hl=en)

---

## 📦 Development

```bash
# Editable install with dev tools
pip install -e ".[dev]"

# Format code
black queuectl tests scripts

# Sort imports
isort queuectl tests scripts

# Run all tests
pytest
```

---

<p align="center">
  Built with ❤️ using Python · Click · SQLAlchemy · Rich
</p>
