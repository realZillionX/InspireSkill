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
from inspire.cli.utils.events import DEFAULT_EVENT_TAIL, run_events_command
from inspire.config import Config, ConfigError
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api.jobs import (
    list_job_events,
    list_job_instance_events,
)
from .job_commands import (
    WebJobResolutionError,
    _close_web_client,
    _reject_job_instance_name,
    _run_readonly_web_job_operation,
    _resolve_web_job_id,
)

_JOB_INSTANCE_PAGE_SIZE = 200


def _list_all_job_instance_names(job_id: str, *, session) -> list[str]:  # noqa: ANN001
    """Page through every instance or fail instead of returning a partial scope."""
    names: list[str] = []
    seen: set[str] = set()
    page_num = 1
    while True:
        instances, total = browser_api_module.list_job_instances(
            job_id,
            limit=_JOB_INSTANCE_PAGE_SIZE,
            page_num=page_num,
            session=session,
        )
        added = 0
        for item in instances:
            name = str(item.get("name") or "").strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)
                added += 1
        if not instances or added == 0:
            if len(names) >= total:
                return names
            raise RuntimeError("Could not retrieve the complete job instance list.")
        if len(instances) < _JOB_INSTANCE_PAGE_SIZE:
            if len(names) >= total:
                return names
            raise RuntimeError("Could not retrieve the complete job instance list.")
        page_num += 1


@click.command("events")
@click.argument("job")
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
    "instance_names",
    multiple=True,
    help=(
        "Query per-pod events (scheduler view: `FailedScheduling` / `Scheduled` / "
        "`Pulling` / `Started`) for the given instance name(s). Can be repeated. "
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
    type=click.IntRange(1),
    help=f"Show the latest {DEFAULT_EVENT_TAIL} events by default; use --tail N to change the limit.",
)
@click.option("--follow", "-f", is_flag=True, help="Follow the event timeline and print new events.")
@click.option(
    "--interval",
    type=click.IntRange(1),
    default=5,
    show_default=True,
    help="Polling interval in seconds for --follow.",
)
@click.option("--workspace", required=True, help="Workspace name or 'all'.")
@pass_context
def events(
    ctx: Context,
    job: str,
    type_filter: Optional[str],
    reason_filter: Optional[str],
    instance_names: tuple[str, ...],
    all_instances: bool,
    tail: Optional[int],
    follow: bool,
    interval: int,
    workspace: Optional[str],
) -> None:
    """Show events for a training job.

    \b
    Examples:
      inspire job events <job-name> --workspace 分布式训练空间
      inspire --json job events <job-name> --workspace 分布式训练空间
      inspire job events <job-name> --workspace 分布式训练空间 --type Warning
      inspire job events <job-name> --workspace 分布式训练空间 --reason Unschedulable
      inspire job events <job-name> --workspace 分布式训练空间 --instance <instance-name>
      inspire job events <job-name> --workspace 分布式训练空间 --follow
      inspire job events <job-name> --workspace all --all-instances
    """
    pods = (
        [_reject_job_instance_name(ctx, value) for value in instance_names]
        if instance_names
        else None
    )
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        resolved_id = _resolve_web_job_id(
            config=config,
            job=job,
            workspace=workspace,
            all_workspaces=False,
            max_pages=50,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except WebJobResolutionError as e:
        _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
        return

    if all_instances:
        event_scope_key = f"{job}__all_instances"
    elif pods:
        event_scope_key = f"{job}__{'_'.join(p.rsplit('/', 1)[-1] for p in pods)}"
    else:
        event_scope_key = job

    def _fetch_web_events() -> list[dict]:
        try:
            def _fetch(resolved_id: str, session) -> list[dict]:  # noqa: ANN001
                if all_instances:
                    pod_names = _list_all_job_instance_names(
                        resolved_id,
                        session=session,
                    )
                    return list_job_instance_events(
                        resolved_id,
                        pod_names,
                        session=session,
                    )
                if pods:
                    return list_job_instance_events(
                        resolved_id,
                        pods,
                        session=session,
                    )
                return list_job_events(resolved_id, session=session)

            return _run_readonly_web_job_operation(
                config=config,
                job=job,
                workspace=workspace,
                operation=_fetch,
            )
        finally:
            _close_web_client()

    def _fetch_local_events() -> list[dict]:
        return list_job_instance_events(resolved_id, pods) if pods else list_job_events(resolved_id)

    run_events_command(
        ctx,
        resource_id=event_scope_key,
        resource_type="job",
        resource_name=job,
        fetch=_fetch_web_events,
        json_output_local=False,
        type_filter=type_filter,
        reason_filter=reason_filter,
        tail=tail,
        follow=follow,
        interval=interval,
    )
