import json

import click

from . import config as config_mod
from . import database
from . import queue_ops
from . import worker_manager
from .exceptions import QueueCTLError
from .models import State


def _session():
    return database.get_session()


def _job_row(job) -> str:
    return (
        f"{job.id:<14} {job.state:<10} attempts={job.attempts}/{job.max_retries:<3} "
        f"prio={job.priority:<3} cmd={job.command!r}"
    )


@click.group()
def main():
    """queuectl - a CLI-based background job queue with workers, retries, and a DLQ."""


@main.command()
@click.argument("job_json")
def enqueue(job_json):
    """Add a new job to the queue.

    JOB_JSON is a JSON object with at least a "command" field, e.g.
    queuectl enqueue '{"id":"job1","command":"sleep 2"}'
    """
    try:
        data = json.loads(job_json)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"Invalid JSON: {exc}")
    if not isinstance(data, dict):
        raise click.ClickException("Job payload must be a JSON object")

    session = _session()
    try:
        job = queue_ops.create_job(session, data)
        message = f"Enqueued job {job.id} (state={job.state}, max_retries={job.max_retries})"
    except QueueCTLError as exc:
        raise click.ClickException(str(exc))
    finally:
        session.close()
    click.echo(message)


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
        lines = [_job_row(job) for job in jobs]
    finally:
        session.close()
    if not lines:
        click.echo("No jobs found.")
        return
    for line in lines:
        click.echo(line)


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
