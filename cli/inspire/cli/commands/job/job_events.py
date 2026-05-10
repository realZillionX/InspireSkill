"""`inspire job events <name>` — platform events for a GPU job.

The command supports job-level events and optional per-pod events via
``--instance`` / ``--all-instances``. Human output is meant for diagnosis:
scheduling failures, image pulls, container starts, and related lifecycle
messages. Events are always fetched from the live platform API.
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
    help="Fetch per-pod events for every instance in the job.",
)
@click.option(
    "--tail",
    type=int,
    help="Show only the last N events (applied after --type / --reason).",
)
@click.option("--web", is_flag=True, help="Use the platform detail view.")
@click.option("--workspace", default=None, help="Workspace name")
@click.option(
    "--all-workspaces",
    "-A",
    is_flag=True,
    help="Search all visible workspaces when resolving a job name",
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
    type_filter: Optional[str],
    reason_filter: Optional[str],
    instance_ids: tuple[str, ...],
    all_instances: bool,
    tail: Optional[int],
    web: bool,
    workspace: Optional[str],
    all_workspaces: bool,
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
      inspire job events --web <job-name>
      inspire job events -A <job-name> --all-instances
    """
    web_mode = web or workspace or all_workspaces or all_instances

    if web_mode:
        try:
            config, _ = Config.from_files_and_env(require_credentials=False)
            resolved_id = _resolve_web_job_id(
                config=config,
                job=job,
                workspace=workspace,
                all_workspaces=all_workspaces,
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
        event_scope_key = f"{resolved_id}__all_instances"
    elif pods:
        event_scope_key = f"{resolved_id}__{'_'.join(p.rsplit('/', 1)[-1] for p in pods)}"
    else:
        event_scope_key = resolved_id

    def _fetch_web_events() -> list[dict]:
        try:
            session = get_web_session()
            if all_instances:
                instances, _ = browser_api_module.list_job_instances(
                    resolved_id,
                    num=200,
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
        resource_id=event_scope_key,
        resource_type="job",
        resource_name=job,
        fetch=_fetch_web_events if web_mode else _fetch_local_events,
        json_output_local=json_output_local,
        type_filter=type_filter,
        reason_filter=reason_filter,
        tail=tail,
    )
