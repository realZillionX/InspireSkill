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
from inspire.cli.utils.id_resolver import NAME_PICK_HELP
from inspire.cli.utils.raw_ids import scrub_raw_ids
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
    _reject_web_job_name_at_boundary,
    _run_readonly_web_job_operation,
)

_JOB_INSTANCE_PAGE_SIZE = 200


def _labelled_instance_events(events: list[dict]) -> list[dict]:
    """Name each per-pod row with the instance it belongs to.

    `--all-instances` concatenates every pod's events into one timeline, and
    the only field that says which pod a row came from is ``object_id`` — the
    same instance name `inspire job instances` prints, dropped by the shared
    public projection. Attaching it here is what makes "which worker failed to
    schedule" answerable from the output rather than from a second query.
    """
    labelled: list[dict] = []
    for event in events:
        row = dict(event)
        name = str(row.get("object_id") or "").strip()
        if name:
            row["instance"] = scrub_raw_ids(name)
        labelled.append(row)
    return labelled


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
@click.argument("job", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
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
    metavar="REASON",
    help="Filter events whose `reason` contains this substring (case-insensitive).",
)
@click.option(
    "--instance",
    "instance_names",
    multiple=True,
    metavar="NAME",
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
    default=DEFAULT_EVENT_TAIL,
    show_default=True,
    help="Maximum recent events to display.",
)
@click.option("--follow", "-f", is_flag=True, help="Follow the event timeline and print new events.")
@click.option(
    "--interval",
    type=click.IntRange(1),
    default=5,
    show_default=True,
    help="Polling interval in seconds for --follow.",
)
@pass_context
def events(
    ctx: Context,
    job: str,
    workspace: Optional[str],
    pick: Optional[int],
    type_filter: Optional[str],
    reason_filter: Optional[str],
    instance_names: tuple[str, ...],
    all_instances: bool,
    tail: int,
    follow: bool,
    interval: int,
) -> None:
    """Show events for a training job.

    \b
    Examples:
      inspire job events train-a --workspace 分布式训练空间
      inspire --json job events train-a --workspace 分布式训练空间
      inspire job events train-a --workspace 分布式训练空间 --type Warning
      inspire job events train-a --workspace 分布式训练空间 --reason Unschedulable
      inspire job events train-a --workspace 分布式训练空间 --instance worker-0
      inspire job events train-a --workspace 分布式训练空间 --follow
    """
    job = _reject_web_job_name_at_boundary(ctx, job)
    pods = (
        [_reject_job_instance_name(ctx, value) for value in instance_names]
        if instance_names
        else None
    )
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return

    def _fetch_web_events() -> list[dict]:
        try:
            def _fetch(resolved_id: str, session) -> list[dict]:  # noqa: ANN001
                if all_instances:
                    pod_names = _list_all_job_instance_names(
                        resolved_id,
                        session=session,
                    )
                    return _labelled_instance_events(
                        list_job_instance_events(
                            resolved_id,
                            pod_names,
                            session=session,
                        )
                    )
                if pods:
                    return _labelled_instance_events(
                        list_job_instance_events(
                            resolved_id,
                            pods,
                            session=session,
                        )
                    )
                return list_job_events(resolved_id, session=session)

            try:
                return _run_readonly_web_job_operation(
                    job=job,
                    workspace=workspace,
                    pick=pick,
                    workspace_must_be_single=True,
                    operation=_fetch,
                )
            except ConfigError as e:
                _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
            except WebJobResolutionError as e:
                _handle_error(ctx, "JobNotFound", str(e), EXIT_JOB_NOT_FOUND)
            return []
        finally:
            _close_web_client()

    run_events_command(
        ctx,
        fetch=_fetch_web_events,
        type_filter=type_filter,
        reason_filter=reason_filter,
        tail=tail,
        follow=follow,
        interval=interval,
    )
