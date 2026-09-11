"""`inspire hpc events <name>` — platform events for an HPC job.

The default is the whole picture: job-level controller events merged with the
per-pod scheduler and kubelet view (``Scheduled`` / ``Pulling`` / ``Created`` /
``Started`` / ``BackOff`` / ``Failed``), which is where a job that never
produced a log line explains itself. The two are disjoint sets from two
different Actions, so reading only one of them answers half the question.
``--instance`` narrows to a role.

Instances are addressed by the Role / Rank identity that
`inspire hpc instances` prints, not by pod name: HPC pod names are platform
handles and never cross the output boundary.
"""

from __future__ import annotations

from inspire.services.hpc.hpc_events import (
    collapse_repeated_events as _collapse_repeated_events,
    labelled_events as _labelled_events,
)

from typing import Optional

import click

from inspire.cli.context import Context, EXIT_CONFIG_ERROR, EXIT_VALIDATION_ERROR, pass_context
from inspire.cli.commands.hpc.hpc_commands import (
    HPCInstanceSelectionError,
    _fetch_hpc_instances,
    _reject_hpc_name_at_boundary,
    _run_readonly_hpc_operation,
    hpc_instance_views,
    select_hpc_instance_views,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.events import DEFAULT_EVENT_TAIL, event_sort_key, run_events_command
from inspire.cli.utils.id_resolver import NAME_PICK_HELP
from inspire.config import Config, ConfigError
from inspire.platform.web.browser_api.hpc_jobs import (
    list_hpc_instance_events,
    list_hpc_job_events,
)
from inspire.platform.web.session import get_web_session

_INSTANCE_SCAN_LIMIT = 500
_COLLAPSE_FIELDS = (
    "object_id",
    "object_type",
    "reason",
    "message",
    "from",
    "first_timestamp",
    "last_timestamp",
)


def _instance_events(
    job_id: str,
    live_session,  # noqa: ANN001
    *,
    selectors: tuple[str, ...],
) -> list[dict]:
    """Fetch per-pod events for the selected instances (unordered)."""
    instances, _total = _fetch_hpc_instances(
        job_id,
        limit=_INSTANCE_SCAN_LIMIT,
        session=live_session,
        show_all=True,
    )
    views = select_hpc_instance_views(hpc_instance_views(instances), selectors)
    events = list_hpc_instance_events(
        [view.handle for view in views],
        live_session,
        job_id=job_id,
    )
    return _labelled_events(events, views)


@click.command("events")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@click.option(
    "--reason",
    "reason_filter",
    metavar="REASON",
    help="Filter events whose `reason` contains this substring (case-insensitive).",
)
@click.option(
    "--instance",
    "instance_selectors",
    multiple=True,
    metavar="ROLE",
    help=(
        "Narrow to one instance, named by the Role (and Rank, when a role has "
        "replicas) column of `inspire hpc instances` — for example slurmd. "
        "Repeat for several. Default: controller events plus every instance."
    ),
)
@click.option(
    "--workload-level",
    "workload_level",
    is_flag=True,
    help=(
        "Only the controller's own events about the job as a whole. "
        "Cannot be combined with --instance."
    ),
)
@click.option(
    "--tail",
    type=click.IntRange(1),
    default=DEFAULT_EVENT_TAIL,
    show_default=True,
    help="Maximum recent events to display.",
)
@click.option(
    "--follow",
    "-f",
    is_flag=True,
    help=(
        "Follow the event timeline and print new events. Runs until interrupted; it never exits on its own, "
        "not even once the job reaches a terminal state."
    ),
)
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
    name: str,
    workspace: str,
    pick: Optional[int],
    reason_filter: Optional[str],
    instance_selectors: tuple[str, ...],
    workload_level: bool,
    tail: int,
    follow: bool,
    interval: int,
) -> None:
    """Show platform events for an HPC job.

    Controller events and every instance's pod events are merged into one
    timeline, so a job that never started explains itself in one call. Use
    ``--instance`` to narrow to a single role, or ``--workload-level`` to keep
    only the controller's half.

    \b
    Examples:
      inspire hpc events prep-a --workspace CPU资源空间
      inspire --json hpc events prep-a --workspace CPU资源空间
      inspire hpc events prep-a --workspace CPU资源空间 --reason Deleted
      inspire hpc events prep-a --workspace CPU资源空间 --instance slurmd
      inspire hpc events prep-a --workspace CPU资源空间 --workload-level
      inspire hpc events prep-a --workspace CPU资源空间 --follow
    """
    name = _reject_hpc_name_at_boundary(ctx, name)
    if workload_level and instance_selectors:
        _handle_error(
            ctx,
            "InvalidUsage",
            "--workload-level and --instance cannot be used together.",
            EXIT_VALIDATION_ERROR,
        )
        return
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return

    def _fetch(resolved_id: str, live_session) -> list[dict]:  # noqa: ANN001
        if workload_level:
            return sorted(
                list_hpc_job_events(resolved_id, session=live_session),
                key=event_sort_key,
            )
        instance_events = _instance_events(
            resolved_id,
            live_session,
            selectors=instance_selectors,
        )
        if instance_selectors:
            return sorted(instance_events, key=event_sort_key)
        merged = list_hpc_job_events(resolved_id, session=live_session) + instance_events
        return sorted(merged, key=event_sort_key)

    def _fetch_events() -> list[dict]:
        try:
            return _collapse_repeated_events(
                _run_readonly_hpc_operation(
                    ctx,
                    session=session,
                    name=name,
                    workspace=workspace,
                    limit=10000,
                    pick=pick,
                    operation=_fetch,
                )
            )
        except HPCInstanceSelectionError as e:
            _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
            return []

    run_events_command(
        ctx,
        fetch=_fetch_events,
        type_filter=None,  # HPC events lack `type`; filter not applicable
        reason_filter=reason_filter,
        tail=tail,
        follow=follow,
        interval=interval,
    )
