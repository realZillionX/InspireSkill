"""`inspire job events <id>` — K8s events for a distributed training job.

Two modes:

* **Default (job-level)** — `POST /api/v1/train_job/job_event_list`, returns
  controller-level events (pytorchjob-controller reporting `Unschedulable`
  / `SetPodTemplateSchedulerName` etc.).
* **`--instance <pod>` (per-pod)** — `POST /api/v1/train_job/events/list`
  with `object_type=instance`, returns scheduler view on specific pods
  (`FailedScheduling` / `Scheduled` / `Pulling` / `Started`).

Both paths cache to `~/.inspire/events/<job_id>.events.json` (per-instance
writes into `<job_id>__<pod>.events.json` so multiple pods don't clobber
each other).
"""

from __future__ import annotations

from typing import Optional

import click

from inspire.cli.context import Context, pass_context
from inspire.cli.utils.events import run_events_command
from inspire.cli.utils.job_cli import resolve_job_id
from inspire.platform.web.browser_api.jobs import (
    list_job_events,
    list_job_instance_events,
)


@click.command("events")
@click.argument("job")
@click.option(
    "--json",
    "json_output_local",
    is_flag=True,
    help="Output as JSON. Equivalent to top-level `--json`.",
)
@click.option(
    "--from-cache",
    is_flag=True,
    help="Read from `~/.inspire/events/<id>.events.json` and skip the live fetch.",
)
@click.option(
    "--type",
    "type_filter",
    type=click.Choice(["Normal", "Warning"], case_sensitive=False),
    help="Filter by K8s event type.",
)
@click.option(
    "--reason",
    "reason_filter",
    help="Filter events whose `reason` contains this substring (case-insensitive).",
)
@click.option(
    "--instance",
    "instance_ids",
    multiple=True,
    help=(
        "Query per-pod events (scheduler view: `FailedScheduling` / `Scheduled` / "
        "`Pulling` / `Started`) for the given pod name(s). Can be repeated. "
        "Without this flag, job-level controller events are returned instead."
    ),
)
@click.option(
    "--tail",
    type=int,
    help="Show only the last N events (applied after --type / --reason).",
)
@pass_context
def events(
    ctx: Context,
    job: str,
    json_output_local: bool,
    from_cache: bool,
    type_filter: Optional[str],
    reason_filter: Optional[str],
    instance_ids: tuple[str, ...],
    tail: Optional[int],
) -> None:
    """Show events for a training job.

    \b
    Examples:
      inspire job events <job-name>
      inspire --json job events <job-name>
      inspire job events <job-name> --type Warning
      inspire job events <job-name> --reason Unschedulable
      inspire job events <job-name> --instance <pod-name>
      inspire job events <job-name> --from-cache
    """
    resolved_id = resolve_job_id(ctx, job)
    pods = list(instance_ids) if instance_ids else None
    if pods:
        # per-instance cache key includes pod names (hash on the fly to keep path short)
        cache_key = f"{resolved_id}__{'_'.join(p.rsplit('/', 1)[-1] for p in pods)}"
    else:
        cache_key = resolved_id

    run_events_command(
        ctx,
        job_id=cache_key,
        fetch=(
            (lambda: list_job_instance_events(resolved_id, pods))
            if pods
            else (lambda: list_job_events(resolved_id))
        ),
        json_output_local=json_output_local,
        from_cache=from_cache,
        type_filter=type_filter,
        reason_filter=reason_filter,
        tail=tail,
    )
