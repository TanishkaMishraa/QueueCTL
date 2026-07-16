import json

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import config as config_mod
from . import database
from . import queue_ops
from . import worker_manager
from .exceptions import JobNotFoundError, QueueCTLError
from .models import State

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
        raise click.ClickException(
            f"Invalid --priority {value!r}: use an integer, or one of low/normal/high"
        )


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
@click.option("--max-retries", "max_retries", default=None, type=int, help="Max attempts before moving to the DLQ.")
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
    except QueueCTLError as exc:
        raise click.ClickException(str(exc))
    finally:
        session.close()
    _console().print(panel)


@main.group()
def worker():
    """Manage worker processes."""


@worker.command("start")
@click.option("--count", default=1, show_default=True, help="Number of worker processes to start.")
@click.option("--foreground", is_flag=True, help="Run a single worker in the foreground (blocks; Ctrl+C to stop).")
def worker_start(count, foreground):
    """Start one or more workers."""
    try:
        worker_ids = worker_manager.start_workers(count, foreground=foreground)
    except ValueError as exc:
        raise click.ClickException(str(exc))
    if not foreground:
        click.echo(f"Started {len(worker_ids)} worker(s): {', '.join(worker_ids)}")


@worker.command("stop")
@click.option("--timeout", default=10.0, show_default=True, help="Seconds to wait for workers to finish their current job.")
def worker_stop(timeout):
    """Stop running workers gracefully (finishes in-flight jobs first)."""
    result = worker_manager.stop_workers(timeout=timeout)
    if result["requested"] == 0:
        click.echo("No running workers found.")
        return
    click.echo(f"Stop requested for {result['requested']} worker(s); {result['stopped']} confirmed stopped.")
    if result["stopped"] < result["requested"]:
        click.echo("Some workers did not confirm within the timeout; they will stop after their current job.")


@main.command()
def status():
    """Show summary of all job states & active workers."""
    session = _session()
    try:
        summary = queue_ops.status_summary(session)
        worker_lines = [
            f"  {w.worker_id:<14} pid={w.pid:<8} status={w.status:<8} "
            f"current_job={(w.current_job_id or '-'):<14} last_heartbeat={w.last_heartbeat}"
            for w in summary["workers"]
        ]
    finally:
        session.close()

    click.echo(f"Total jobs: {summary['jobs_total']}")
    for state in State.ALL:
        click.echo(f"  {state:<10} {summary['by_state'][state]}")

    click.echo(f"\nAttempts logged: {summary['total_attempts_logged']}  "
               f"Success rate: {summary['success_rate_pct']}%")

    click.echo("\nWorkers:")
    if not worker_lines:
        click.echo("  (none)")
    for line in worker_lines:
        click.echo(line)


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
    click.echo(f"Deleted job {job_id}")


@main.group()
def dlq():
    """View or retry Dead Letter Queue jobs."""


@dlq.command("list")
def dlq_list_cmd():
    """List jobs that permanently failed (moved to the DLQ)."""
    session = _session()
    try:
        jobs = queue_ops.dlq_list(session)
        lines = [f"{_job_row(job)} last_error={job.last_error!r}" for job in jobs]
    finally:
        session.close()
    if not lines:
        click.echo("DLQ is empty.")
        return
    for line in lines:
        click.echo(line)


@dlq.command("retry")
@click.argument("job_id")
def dlq_retry_cmd(job_id):
    """Move a DLQ job back to pending for another attempt."""
    session = _session()
    try:
        try:
            job = queue_ops.dlq_retry(session, job_id)
            message = f"Job {job.id} requeued (state={job.state})"
        except QueueCTLError as exc:
            raise click.ClickException(str(exc))
    finally:
        session.close()
    click.echo(message)


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
        config_mod.set_config(session, key.replace("-", "_"), value)
    finally:
        session.close()
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


@config.command("list")
def config_list():
    """List all configuration values."""
    session = _session()
    try:
        for k, v in config_mod.get_all(session).items():
            click.echo(f"{k} = {v}")
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
    click.echo(f"Reset {key}" if key else "Reset all configuration values to defaults")


if __name__ == "__main__":
    main()
