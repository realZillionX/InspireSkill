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

from inspire.cli.context import Context, EXIT_CONFIG_ERROR, EXIT_JOB_NOT_FOUND, pass_context
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.events import run_events_command
from inspire.cli.utils.job_cli import resolve_job_id
from inspire.config import Config, ConfigError
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api.jobs import (
    list_job_events,
    list_job_instance_events,
)
from inspire.platform.web.session import get_web_session

from .job_commands import WebJobResolutionError, _close_web_client, _resolve_web_job_id


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
    "--all-instances",
    is_flag=True,
    help="In web mode, fetch per-pod events for every instance in the job.",
)
@click.option(
    "--tail",
    type=int,
    help="Show only the last N events (applied after --type / --reason).",
)
@click.option("--web", is_flag=True, help="Resolve the job via the web UI API")
@click.option("--workspace", default=None, help="Workspace alias or ws-... id (web mode)")
@click.option(
    "--all-workspaces",
    "-A",
    is_flag=True,
    help="Search all visible workspaces when resolving a web job name",
)
@click.option(
    "--all-users",
    is_flag=True,
    help="Include jobs from all users when resolving a web job name",
)
@click.option(
    "--created-by", default=None, help="Filter web job name resolution by creator user ID"
)
@click.option(
    "--max-pages",
    type=int,
    default=50,
    show_default=True,
    help="Max web pages to scan per workspace when resolving a job name",
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
    all_instances: bool,
    tail: Optional[int],
    web: bool,
    workspace: Optional[str],
    all_workspaces: bool,
    all_users: bool,
    created_by: Optional[str],
    max_pages: int,
) -> None:
    """Show events for a training job.

    \b
    Examples:
      inspire job events <job-name>
      inspire --json job events <job-name>
      inspire job events <job-name> --type Warning
      inspire job events <job-name> --reason Unschedulable
      inspire job events <job-name> --instance <pod-name>
      inspire job events --web job-...
      inspire job events -A <job-name> --all-instances
      inspire job events <job-name> --from-cache
    """
    web_mode = web or workspace or all_workspaces or all_users or created_by or all_instances

    if web_mode:
        try:
            config, _ = Config.from_files_and_env(require_credentials=False)
            resolved_id = _resolve_web_job_id(
                config=config,
                job=job,
                workspace=workspace,
                all_workspaces=all_workspaces,
                all_users=all_users,
                created_by=created_by,
                max_pages=max_pages,
            )
        except ConfigError as e:
            _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            return
        except WebJobResolutionError as e:
            _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
            return
    else:
        resolved_id = resolve_job_id(ctx, job)

    pods = list(instance_ids) if instance_ids else None
    if all_instances:
        cache_key = f"{resolved_id}__all_instances"
    elif pods:
        # per-instance cache key includes pod names (hash on the fly to keep path short)
        cache_key = f"{resolved_id}__{'_'.join(p.rsplit('/', 1)[-1] for p in pods)}"
    else:
        cache_key = resolved_id

    def _fetch_web_events() -> list[dict]:
        try:
            session = get_web_session()
            if all_instances:
                instances, _ = browser_api_module.list_job_instances(
                    resolved_id,
                    page_num=1,
                    page_size=200,
                    session=session,
                )
                pod_names = [
                    str(item.get("name") or "").strip()
                    for item in instances
                    if str(item.get("name") or "").strip()
                ]
                return list_job_instance_events(resolved_id, pod_names, session=session)
            if pods:
                return list_job_instance_events(resolved_id, pods, session=session)
            return list_job_events(resolved_id, session=session)
        finally:
            _close_web_client()

    def _fetch_local_events() -> list[dict]:
        return list_job_instance_events(resolved_id, pods) if pods else list_job_events(resolved_id)

    run_events_command(
        ctx,
        job_id=cache_key,
        fetch=_fetch_web_events if web_mode else _fetch_local_events,
        json_output_local=json_output_local,
        from_cache=from_cache,
        type_filter=type_filter,
        reason_filter=reason_filter,
        tail=tail,
    )
