import json
import sys
import time

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import config as config_mod
from . import constants, database
from . import dlq as dlq_mod
from . import metrics as metrics_mod
from . import queue_ops, worker_manager
from .app_logging import get_app_logger
from .exceptions import JobNotFoundError, QueueCTLError
from .models import State
from .utils import utcnow

# Fixed width avoids Rich wrapping/truncating output differently depending
# on whether stdout is a real terminal, a pipe, or captured by tests.
_CONSOLE_WIDTH = 100

_PRIORITY_ALIASES = {"low": -10, "normal": 0, "high": 10}


def _session():
    return database.get_session()


def _console() -> Console:
    return Console(width=_CONSOLE_WIDTH)


def _parse_priority(value):
    text = str(value).strip().lower()
    if text in _PRIORITY_ALIASES:
        return _PRIORITY_ALIASES[text]
    try:
        return int(value)
    except (TypeError, ValueError):
        raise click.ClickException(f"Invalid --priority {value!r}: use an integer, or one of low/normal/high")


def _is_worker_stale(w) -> bool:
    age_seconds = (utcnow() - w.last_heartbeat).total_seconds()
    return w.status == "running" and age_seconds > constants.HEARTBEAT_STALE_SECONDS


def _format_worker_line(w) -> str:
    age_seconds = (utcnow() - w.last_heartbeat).total_seconds()
    job = w.current_job_id or "-"
    line = (
        f"  {w.worker_id:<14} pid={w.pid:<8} status={w.status:<8} "
        f"current_job={job:<14} heartbeat={age_seconds:.0f}s ago"
    )
    if _is_worker_stale(w):
        line += "  [STALE - no heartbeat, likely crashed]"
    return line


def _job_row(job) -> str:
    return (
        f"{job.id:<14} {job.state:<10} attempts={job.attempts}/{job.max_retries:<3} "
        f"prio={job.priority:<3} cmd={job.command!r}"
    )


def _jobs_table(jobs) -> Table:
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID")
    table.add_column("State")
    table.add_column("Attempts")
    table.add_column("Priority")
    table.add_column("Command")
    for job in jobs:
        table.add_row(job.id, job.state, f"{job.attempts}/{job.max_retries}", str(job.priority), job.command)
    return table


def _job_created_panel(job) -> Panel:
    body = (
        f"[bold]ID[/bold]: {job.id}\n"
        f"[bold]State[/bold]: {job.state}\n"
        f"[bold]Max Retries[/bold]: {job.max_retries}\n"
        f"[bold]Priority[/bold]: {job.priority}"
    )
    # Plain ASCII title only: Rich's legacy-Windows-console writer crashes
    # (UnicodeEncodeError under the default cp1252 codepage) on non-ASCII
    # glyphs like a checkmark, and CliRunner's captured-output tests don't
    # exercise that real-console code path, so this can't be caught by the
    # test suite -- it only surfaces when actually run in a terminal.
    return Panel(body, title="[green]Job Created[/green]", expand=False)


def _job_details_panel(job) -> Panel:
    lines = [
        f"[bold]ID[/bold]: {job.id}",
        f"[bold]Command[/bold]: {job.command}",
        f"[bold]State[/bold]: {job.state}",
        f"[bold]Attempts[/bold]: {job.attempts}/{job.max_retries}",
        f"[bold]Priority[/bold]: {job.priority}",
        f"[bold]Created[/bold]: {job.created_at}",
        f"[bold]Updated[/bold]: {job.updated_at}",
    ]
    if job.run_at:
        lines.append(f"[bold]Run At[/bold]: {job.run_at}")
    if job.timeout_seconds:
        lines.append(f"[bold]Timeout[/bold]: {job.timeout_seconds}s")
    if job.last_error:
        lines.append(f"[bold]Last Error[/bold]: {job.last_error}")
    return Panel("\n".join(lines), title=f"Job {job.id}", expand=False)


@click.group()
def main():
    """queuectl - a CLI-based background job queue with workers, retries, and a DLQ."""


@main.command()
@click.argument("job_arg")
@click.option("--id", "job_id", default=None, help="Custom job id (default: auto-generated).")
@click.option("--priority", default=None, help="Integer, or one of low/normal/high (default: normal/0).")
@click.option("--timeout", "timeout_seconds", default=None, type=int, help="Per-job timeout in seconds.")
@click.option(
    "--max-retries", "max_retries", default=None, type=int, help="Max attempts before moving to the DLQ."
)
@click.option("--run-at", default=None, help="ISO timestamp; job won't be claimed before this time.")
def enqueue(job_arg, job_id, priority, timeout_seconds, max_retries, run_at):
    """Add a new job to the queue.

    JOB_ARG is either a plain shell command:

        queuectl enqueue "echo Hello World" --priority high --timeout 30

    or a JSON object, matching the assignment's original example:

        queuectl enqueue '{"id":"job1","command":"sleep 2"}'
    """
    stripped = job_arg.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise click.ClickException(f"Invalid JSON: {exc}")
        if not isinstance(data, dict):
            raise click.ClickException("Job payload must be a JSON object")
    else:
        data = {"command": job_arg}

    if job_id is not None:
        data.setdefault("id", job_id)
    if priority is not None:
        data["priority"] = _parse_priority(priority)
    if timeout_seconds is not None:
        data["timeout_seconds"] = timeout_seconds
    if max_retries is not None:
        data["max_retries"] = max_retries
    if run_at is not None:
        data["run_at"] = run_at

    session = _session()
    try:
        job = queue_ops.create_job(session, data)
        panel = _job_created_panel(job)
        job_id = job.id
    except QueueCTLError as exc:
        raise click.ClickException(str(exc))
    finally:
        session.close()
    get_app_logger().info(f"Job {job_id} created (command={data.get('command')!r})")
    _console().print(panel)


@main.group()
def worker():
    """Manage worker processes."""


@worker.command("start")
@click.option("--count", default=1, show_default=True, help="Number of worker processes to start.")
@click.option(
    "--foreground", is_flag=True, help="Run a single worker in the foreground (blocks; Ctrl+C to stop)."
)
def worker_start(count, foreground):
    """Start one or more workers."""
    session = _session()
    try:
        max_workers = config_mod.get_int(session, "max_workers")
        running_now = sum(
            1 for w in queue_ops.list_workers(session) if w.status == "running" and not _is_worker_stale(w)
        )
    finally:
        session.close()
    if running_now + count > max_workers:
        raise click.ClickException(
            f"Refusing to start {count} worker(s): {running_now} already running and "
            f"max_workers={max_workers} (queuectl config set max-workers N to raise this)."
        )

    try:
        worker_ids = worker_manager.start_workers(count, foreground=foreground)
    except ValueError as exc:
        raise click.ClickException(str(exc))
    if not foreground:
        click.echo(f"Started {len(worker_ids)} worker(s): {', '.join(worker_ids)}")


@worker.command("stop")
@click.option(
    "--timeout",
    default=10.0,
    show_default=True,
    help="Seconds to wait for workers to finish their current job.",
)
def worker_stop(timeout):
    """Stop running workers gracefully (finishes in-flight jobs first)."""
    result = worker_manager.stop_workers(timeout=timeout)
    if result["requested"] == 0:
        click.echo("No running workers found.")
        return
    click.echo(f"Stop requested for {result['requested']} worker(s); {result['stopped']} confirmed stopped.")
    if result["stopped"] < result["requested"]:
        click.echo("Some workers did not confirm within the timeout; they will stop after their current job.")


@worker.command("list")
def worker_list_cmd():
    """List worker processes and their status (a focused view of what `status` also shows)."""
    session = _session()
    try:
        workers = queue_ops.list_workers(session)
        lines = [_format_worker_line(w) for w in workers]
        running_count = sum(1 for w in workers if w.status == "running")
    finally:
        session.close()
    click.echo(f"Workers Running: {running_count}")
    if not lines:
        click.echo("  (none)")
    for line in lines:
        click.echo(line)


@main.command()
def status():
    """Show summary of all job states & active workers."""
    session = _session()
    try:
        summary = queue_ops.status_summary(session)
        worker_lines = [_format_worker_line(w) for w in summary["workers"]]
        running_count = sum(1 for w in summary["workers"] if w.status == "running")
    finally:
        session.close()

    click.echo(f"Total jobs: {summary['jobs_total']}")
    for state in State.ALL:
        click.echo(f"  {state:<10} {summary['by_state'][state]}")

    click.echo(
        f"\nAttempts logged: {summary['total_attempts_logged']}  "
        f"Success rate: {summary['success_rate_pct']}%"
    )

    click.echo(f"\nWorkers Running: {running_count}")
    if not worker_lines:
        click.echo("  (none)")
    for line in worker_lines:
        click.echo(line)


@main.command()
def stats():
    """Show job execution statistics: completed/failed/retries/DLQ counts, runtime, and rates."""
    session = _session()
    try:
        m = metrics_mod.calculate_metrics(session)
    finally:
        session.close()

    click.echo("Jobs Executed")
    click.echo(f"  Completed : {m['completed_jobs']}")
    click.echo(f"  Failed    : {m['failed_attempts']}")
    click.echo(f"  Retries   : {m['retry_attempts']}")
    click.echo(f"  DLQ       : {m['dead_jobs']}")

    click.echo(f"\nAverage Runtime : {m['avg_runtime_seconds']:.2f} sec")
    click.echo(f"Longest Runtime : {m['longest_runtime_seconds']:.2f} sec")
    click.echo(f"Shortest Runtime: {m['shortest_runtime_seconds']:.2f} sec")

    click.echo(f"\nSuccess Rate: {m['success_rate_pct']}%")
    click.echo(f"Failure Rate: {m['failure_rate_pct']}%")
    click.echo(f"Retry Rate  : {m['retry_rate_pct']}%")
    click.echo(f"Throughput  : {m['throughput_per_hour']} jobs/hour")


@main.command()
def health():
    """Check database connectivity, worker availability, queue accessibility, and configuration."""
    try:
        session = _session()
    except Exception as exc:
        click.echo("Database")
        click.echo(f"  [FAIL] {exc}")
        click.echo("\nWorkers\n  [FAIL] Unknown (database unreachable)")
        click.echo("\nQueue\n  [FAIL] Not accessible")
        click.echo("\nDLQ\n  [FAIL] Unknown")
        click.echo("\nConfiguration\n  [FAIL] Not loaded")
        sys.exit(1)

    try:
        workers = queue_ops.list_workers(session)
        active_workers = [w for w in workers if w.status == "running" and not _is_worker_stale(w)]
        config_values = config_mod.get_all(session)
        dead_count = dlq_mod.count_dead_jobs(session)
    finally:
        session.close()

    click.echo("Database")
    click.echo("  [OK] Healthy")

    click.echo("\nWorkers")
    click.echo(
        f"  [OK] Running ({len(active_workers)} active)" if active_workers else "  [WARN] None running"
    )

    click.echo("\nQueue")
    click.echo("  [OK] Accepting Jobs")

    click.echo("\nDLQ")
    click.echo(f"  {dead_count} Job(s)")

    click.echo("\nConfiguration")
    click.echo("  [OK] Loaded" if config_values else "  [WARN] Empty")


@main.command()
def dashboard():
    """Rich multi-panel overview: queue stats, current workers, recent jobs, and DLQ health."""
    session = _session()
    try:
        summary = queue_ops.status_summary(session)
        workers = queue_ops.list_workers(session)
        recent_jobs = queue_ops.list_jobs(session, limit=5)
        dead_count = dlq_mod.count_dead_jobs(session)
    finally:
        session.close()

    running_count = sum(1 for w in workers if w.status == "running")
    stats_lines = "\n".join(
        [
            f"Pending      {summary['by_state'][State.PENDING]}",
            f"Processing   {summary['by_state'][State.PROCESSING]}",
            f"Completed    {summary['by_state'][State.COMPLETED]}",
            f"Failed       {summary['by_state'][State.FAILED]}",
            f"Dead         {summary['by_state'][State.DEAD]}",
            f"Workers      {running_count}",
            f"Success Rate {summary['success_rate_pct']}%",
        ]
    )
    stats_panel = Panel(stats_lines, title="QueueCTL Dashboard", expand=False)

    workers_table = Table(title="Current Workers", show_header=True, header_style="bold")
    workers_table.add_column("ID")
    workers_table.add_column("PID")
    workers_table.add_column("Status")
    workers_table.add_column("Heartbeat")
    for w in workers:
        age_seconds = (utcnow() - w.last_heartbeat).total_seconds()
        status_text = w.status + (" (STALE)" if _is_worker_stale(w) else "")
        workers_table.add_row(w.worker_id, str(w.pid), status_text, f"{age_seconds:.0f}s ago")

    jobs_table = Table(title="Recent Jobs", show_header=True, header_style="bold")
    jobs_table.add_column("ID")
    jobs_table.add_column("State")
    jobs_table.add_column("Command")
    for j in recent_jobs:
        jobs_table.add_row(j.id, j.state, j.command)

    console = _console()
    console.print(stats_panel)
    console.print(workers_table)
    console.print(jobs_table)
    console.print(f"Queue Health: DLQ has {dead_count} job(s)")


@main.command()
@click.option(
    "--jobs", "job_count", default=100, show_default=True, help="Number of trivial jobs to enqueue and time."
)
@click.option(
    "--workers", "worker_count", default=4, show_default=True, help="Number of workers to process them."
)
@click.option(
    "--timeout", default=60.0, show_default=True, help="Max seconds to wait for the batch to finish."
)
def benchmark(job_count, worker_count, timeout):
    """Enqueue JOBS trivial jobs, process them with fresh workers, and report throughput.

    Optional/bonus feature -- not part of the assignment's required
    command set. Starts its own workers and stops them again when done,
    independent of any workers you already have running.
    """
    session = _session()
    try:
        max_workers = config_mod.get_int(session, "max_workers")
        if worker_count > max_workers:
            raise click.ClickException(
                f"--workers {worker_count} exceeds max_workers={max_workers} "
                f"(queuectl config set max-workers N to raise this)."
            )
        for _ in range(job_count):
            queue_ops.create_job(session, {"command": "echo bench"})
    finally:
        session.close()

    click.echo(f"Enqueued {job_count} jobs. Starting {worker_count} worker(s)...")
    worker_manager.start_workers(worker_count)

    started = time.monotonic()
    deadline = started + timeout
    session = _session()
    try:
        completed = queue_ops.count_jobs(session, state=State.COMPLETED)
        # Every transaction on this engine opens with BEGIN IMMEDIATE (see
        # database.py), including this read-only count -- committing after
        # each check releases that write lock between polls. Without it,
        # this loop holds the lock for the entire wait and starves every
        # worker trying to claim a job (the same bug worker_manager.stop_workers
        # had to be fixed for; see design.md).
        session.commit()
        while time.monotonic() < deadline and completed < job_count:
            time.sleep(0.2)
            completed = queue_ops.count_jobs(session, state=State.COMPLETED)
            session.commit()
        elapsed = time.monotonic() - started
        m = metrics_mod.calculate_metrics(session)
    finally:
        session.close()

    worker_manager.stop_workers(timeout=10)

    throughput = completed / elapsed if elapsed > 0 else 0.0
    click.echo(f"\n{completed}/{job_count} Jobs Completed")
    click.echo(f"Average Runtime : {m['avg_runtime_seconds']:.2f} sec")
    click.echo(f"\nThroughput      : {throughput:.1f} jobs/sec")
    if completed < job_count:
        click.echo(f"(Timed out after {timeout}s waiting for the remaining {job_count - completed} job(s).)")


@main.command("list")
@click.option("--state", "state", type=click.Choice(State.ALL), default=None, help="Filter by job state.")
@click.option("--limit", default=None, type=int, help="Limit number of results.")
def list_cmd(state, limit):
    """List jobs, optionally filtered by state."""
    session = _session()
    try:
        jobs = queue_ops.list_jobs(session, state=state, limit=limit)
        table = _jobs_table(jobs) if jobs else None
    finally:
        session.close()
    if table is None:
        click.echo("No jobs found.")
        return
    _console().print(table)


@main.group()
def job():
    """Inspect or delete a specific job by id."""


@job.command("show")
@click.argument("job_id")
def job_show(job_id):
    """Show full details for one job."""
    session = _session()
    try:
        existing = queue_ops.get_job(session, job_id)
        if existing is None:
            raise JobNotFoundError(f"Job not found: {job_id}")
        panel = _job_details_panel(existing)
    except QueueCTLError as exc:
        raise click.ClickException(str(exc))
    finally:
        session.close()
    _console().print(panel)


@job.command("delete")
@click.argument("job_id")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
def job_delete(job_id, yes):
    """Delete a job permanently."""
    session = _session()
    try:
        if queue_ops.get_job(session, job_id) is None:
            raise JobNotFoundError(f"Job not found: {job_id}")
        if not yes and not click.confirm(f"Delete job {job_id}?", default=False):
            click.echo("Aborted.")
            return
        queue_ops.delete_job(session, job_id)
    except QueueCTLError as exc:
        raise click.ClickException(str(exc))
    finally:
        session.close()
    get_app_logger().info(f"Job {job_id} deleted")
    click.echo(f"Deleted job {job_id}")


@main.group()
def dlq():
    """View, count, retry, or delete Dead Letter Queue jobs."""


@dlq.command("list")
def dlq_list_cmd():
    """List jobs that permanently failed (moved to the DLQ)."""
    session = _session()
    try:
        jobs = dlq_mod.list_dead_jobs(session)
        lines = [f"{_job_row(job)} last_error={job.last_error!r}" for job in jobs]
    finally:
        session.close()
    if not lines:
        click.echo("DLQ is empty.")
        return
    for line in lines:
        click.echo(line)


@dlq.command("count")
def dlq_count_cmd():
    """Show how many jobs are currently in the DLQ."""
    session = _session()
    try:
        count = dlq_mod.count_dead_jobs(session)
    finally:
        session.close()
    click.echo(f"Dead Jobs: {count}")


@dlq.command("retry")
@click.argument("job_id")
def dlq_retry_cmd(job_id):
    """Move a DLQ job back to pending for another attempt."""
    session = _session()
    try:
        try:
            job = dlq_mod.retry_dead_job(session, job_id)
            message = f"Job {job.id} requeued (state={job.state})"
        except QueueCTLError as exc:
            raise click.ClickException(str(exc))
    finally:
        session.close()
    get_app_logger().info(f"Job {job_id} manually retried from DLQ")
    click.echo(message)


@dlq.command("delete")
@click.argument("job_id")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
def dlq_delete_cmd(job_id, yes):
    """Permanently delete a job from the DLQ."""
    session = _session()
    try:
        if not yes and not click.confirm(f"Delete job {job_id} permanently?", default=False):
            click.echo("Aborted.")
            return
        try:
            dlq_mod.delete_dead_job(session, job_id)
        except QueueCTLError as exc:
            raise click.ClickException(str(exc))
    finally:
        session.close()
    get_app_logger().info(f"Job {job_id} deleted from DLQ")
    click.echo(f"Deleted job {job_id}")


@main.group()
def config():
    """Manage configuration (retry count, backoff base, etc.)."""


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key, value):
    """Set a configuration value, e.g. queuectl config set max-retries 3"""
    session = _session()
    try:
        try:
            config_mod.set_config(session, key.replace("-", "_"), value)
        except QueueCTLError as exc:
            raise click.ClickException(str(exc))
    finally:
        session.close()
    get_app_logger().info(f"Config changed: {key} = {value}")
    click.echo(f"Set {key} = {value}")


@config.command("get")
@click.argument("key", required=False)
def config_get(key):
    """Get one configuration value, or all if no key is given."""
    session = _session()
    try:
        if key:
            try:
                value = config_mod.get_config(session, key.replace("-", "_"))
            except QueueCTLError as exc:
                raise click.ClickException(str(exc))
            click.echo(f"{key} = {value}")
        else:
            for k, v in config_mod.get_all(session).items():
                click.echo(f"{k} = {v}")
    finally:
        session.close()


def _print_all_config(session):
    for k, v in config_mod.get_all(session).items():
        click.echo(f"{k} = {v}")


@config.command("list")
def config_list():
    """List all configuration values."""
    session = _session()
    try:
        _print_all_config(session)
    finally:
        session.close()


@config.command("show")
def config_show():
    """Alias for `config list` (the name this project's phase docs use)."""
    session = _session()
    try:
        _print_all_config(session)
    finally:
        session.close()


@config.command("reset")
@click.argument("key", required=False)
def config_reset(key):
    """Reset one configuration value (or all, if no key given) back to its default."""
    session = _session()
    try:
        normalized = key.replace("-", "_") if key else None
        try:
            config_mod.reset_config(session, normalized)
        except QueueCTLError as exc:
            raise click.ClickException(str(exc))
    finally:
        session.close()
    get_app_logger().info(f"Config reset: {key}" if key else "Config reset: all keys to defaults")
    click.echo(f"Reset {key}" if key else "Reset all configuration values to defaults")


@config.command("delete")
@click.argument("key")
def config_delete(key):
    """Remove a single key's override, falling back to its default (same
    effect as `config reset <key>`, under the name the assignment's
    config-management phase doc uses)."""
    session = _session()
    try:
        try:
            config_mod.delete(session, key.replace("-", "_"))
        except QueueCTLError as exc:
            raise click.ClickException(str(exc))
    finally:
        session.close()
    get_app_logger().info(f"Config changed: {key} deleted (back to default)")
    click.echo(f"Deleted override for {key} (back to default)")


@config.command("export")
@click.argument("path", type=click.Path(dir_okay=False, writable=True))
def config_export(path):
    """Write every configuration value to a JSON file, e.g. for reuse in another deployment."""
    session = _session()
    try:
        config_mod.export_config(session, path)
    finally:
        session.close()
    click.echo(f"Exported configuration to {path}")


@config.command("import")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
def config_import(path):
    """Load configuration values from a JSON file (as produced by `config export`)."""
    session = _session()
    try:
        try:
            config_mod.import_config(session, path)
        except QueueCTLError as exc:
            raise click.ClickException(str(exc))
    finally:
        session.close()
    get_app_logger().info(f"Config changed: imported from {path}")
    click.echo(f"Imported configuration from {path}")


if __name__ == "__main__":
    main()
