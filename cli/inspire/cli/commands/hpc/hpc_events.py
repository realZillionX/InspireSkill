"""`inspire hpc events <name>` — platform events for an HPC job.

Job-level controller events are the default. ``--instance`` / ``--all-instances``
switch to the per-pod scheduler and kubelet view (``Scheduled`` / ``Pulling`` /
``Created`` / ``Started`` / ``BackOff`` / ``Failed``), which is where a job that
never produced a log line explains itself.

Instances are addressed by the Role / Rank identity that
`inspire hpc instances` prints, not by pod name: HPC pod names are platform
handles and never cross the output boundary.
"""

from __future__ import annotations

from typing import Any, Optional

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
from inspire.cli.utils.events import DEFAULT_EVENT_TAIL, run_events_command
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


def _collapse_repeated_events(events: list[dict]) -> list[dict]:
    """Fold byte-identical occurrences into the existing ``count`` column.

    Both HPC event Actions return one row per raw occurrence and never
    populate ``count``: a pod with 20 distinct events answers with 106 rows,
    so a default ``--tail 20`` window can be spent on twenty copies of one
    ``BackOff``. Nothing is dropped — the multiplicity moves into the Count
    column that the shared renderer already has — and a row the platform only
    ever sent once is left untouched so it keeps rendering as before.
    """
    collapsed: dict[tuple[str, ...], dict[str, Any]] = {}
    occurrences: dict[tuple[str, ...], int] = {}
    order: list[tuple[str, ...]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        key = tuple(str(event.get(field) or "") for field in _COLLAPSE_FIELDS)
        if key not in collapsed:
            collapsed[key] = dict(event)
            occurrences[key] = 0
            order.append(key)
        try:
            occurrences[key] += int(str(event.get("count") or 1))
        except ValueError:
            occurrences[key] += 1

    rows: list[dict[str, Any]] = []
    for key in order:
        row = collapsed[key]
        if occurrences[key] > 1:
            row["count"] = occurrences[key]
        rows.append(row)
    return rows


def _event_sort_key(event: dict) -> tuple[int, int]:
    def _epoch(value: object) -> int:
        text = str(value or "").strip()
        return int(text) if text.isdigit() else 0

    return _epoch(event.get("last_timestamp")), _epoch(event.get("first_timestamp"))


def _instance_events(
    job_id: str,
    live_session,  # noqa: ANN001
    *,
    selectors: tuple[str, ...],
) -> list[dict]:
    """Fetch per-pod events for the selected instances, oldest first.

    ``ListSlurmdPodEvent`` accepts no sorter and answers in an arbitrary order,
    and several instances are concatenated here besides — so the ordering that
    makes ``--tail`` mean "most recent" has to be imposed on this side.
    """
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
    return sorted(events, key=_event_sort_key)


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
        "Query per-pod events (`Scheduled` / `Pulling` / `Started` / `BackOff`) "
        "for the instance named by the Role (and Rank, when a role has "
        "replicas) column of `inspire hpc instances` — for example slurmd. "
        "Repeat for several. Without this flag, job-level controller events "
        "are returned instead."
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
    name: str,
    workspace: str,
    pick: Optional[int],
    reason_filter: Optional[str],
    instance_selectors: tuple[str, ...],
    all_instances: bool,
    tail: int,
    follow: bool,
    interval: int,
) -> None:
    """Show platform events for an HPC job.

    \b
    Examples:
      inspire hpc events prep-a --workspace CPU资源空间
      inspire --json hpc events prep-a --workspace CPU资源空间
      inspire hpc events prep-a --workspace CPU资源空间 --reason Deleted
      inspire hpc events prep-a --workspace CPU资源空间 --instance slurmd
      inspire hpc events prep-a --workspace CPU资源空间 --all-instances
      inspire hpc events prep-a --workspace CPU资源空间 --follow
    """
    name = _reject_hpc_name_at_boundary(ctx, name)
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return

    per_instance = all_instances or bool(instance_selectors)
    selectors = () if all_instances else instance_selectors

    def _fetch(resolved_id: str, live_session) -> list[dict]:  # noqa: ANN001
        if per_instance:
            return _instance_events(resolved_id, live_session, selectors=selectors)
        return list_hpc_job_events(resolved_id, session=live_session)

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
