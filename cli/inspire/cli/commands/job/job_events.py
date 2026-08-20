"""`inspire job events <name>` — platform events for a GPU job.

Controller events and per-pod events are two disjoint sets from one Action
(``filter.object_type`` is ``job`` or ``instance``), and the default merges
both: the controller explains why the job was never created, the pods explain
why they were never scheduled or started. ``--instance`` narrows to one, named
by the identity `inspire job instances` prints. Events are always fetched from
the live platform API.
"""

from __future__ import annotations

from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_JOB_NOT_FOUND,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.events import DEFAULT_EVENT_TAIL, event_sort_key, run_events_command
from inspire.cli.utils.id_resolver import NAME_PICK_HELP
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api.jobs import (
    list_job_events,
    list_job_instance_events,
)
from inspire.platform.web.session import get_web_session
from .job_commands import (
    WebJobResolutionError,
    _close_web_client,
    _list_workspace_ids,
    _reject_job_instance_name,
    _reject_web_job_name_at_boundary,
    _resolve_batch_job_ids,
    _run_readonly_web_job_operation,
)
from .job_instances import (
    JobInstanceSelectionError,
    JobInstanceView,
    job_instance_views,
    select_job_instance_views,
)

_JOB_INSTANCE_PAGE_SIZE = 200


def _labelled_instance_events(
    events: list[dict],
    views: list[JobInstanceView],
) -> list[dict]:
    """Name each per-pod row with the instance it belongs to.

    Every pod's events land in one timeline, and the only field that says
    which pod a row came from is ``object_id`` — the handle, which the shared
    public projection drops. Attaching the label here is what makes "which
    worker failed to schedule" answerable from the output rather than from a
    second query. A pod the instance list does not know keeps no label rather
    than falling back to its handle.
    """
    labels = {view.handle: view.label for view in views}
    labelled: list[dict] = []
    for event in events:
        row = dict(event)
        label = labels.get(str(row.get("object_id") or "").strip())
        if label:
            row["instance"] = label
        labelled.append(row)
    return labelled


def _list_all_job_instances(job_id: str, *, session) -> list[dict]:  # noqa: ANN001
    """Page through every instance or fail instead of returning a partial scope."""
    rows: list[dict] = []
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
                rows.append(item)
                added += 1
        if not instances or added == 0:
            if len(rows) >= total:
                return rows
            raise RuntimeError("Could not retrieve the complete job instance list.")
        if len(instances) < _JOB_INSTANCE_PAGE_SIZE:
            if len(rows) >= total:
                return rows
            raise RuntimeError("Could not retrieve the complete job instance list.")
        page_num += 1


def _batch_workload_events(
    names: tuple[str, ...],
    *,
    workspace: Optional[str],
    report_unresolved: bool,
) -> list[dict]:
    """Controller events for several jobs, in one request per 20 jobs.

    Multi-name queries are workload-level by construction: `object_type="job"`
    is the half of the Action that batches, while per-pod events need an
    instance listing per job and would cost one request each anyway. Every row
    is labelled with the job name it came from, because a timeline merged
    across jobs is unreadable without it.

    A name that will not resolve, and a job the platform has since dropped,
    are both reported on stderr and skipped rather than taking the other
    nineteen jobs' events down with them. ``--follow`` re-runs this every few
    seconds, so `report_unresolved` prints that list once rather than once per
    poll.
    """
    session = get_web_session()
    workspace_ids = _list_workspace_ids(session, workspace=workspace)
    if len(workspace_ids) != 1:
        raise ConfigError("--workspace must be a single workspace name for this command.")

    resolved, failures = _resolve_batch_job_ids(names, workspace=workspace)
    events_by_id, missing = browser_api_module.list_job_events_by_ids(
        list(resolved.values()),
        session=session,
    )
    gone = set(missing)
    name_by_id = {job_id: name for name, job_id in resolved.items()}

    rows: list[dict] = []
    for name in names:
        job_id = resolved.get(name)
        if job_id is None:
            continue
        if job_id in gone:
            failures[name] = f"job {name!r} is no longer known to the platform."
            continue
        for event in events_by_id.get(job_id) or []:
            row = dict(event)
            row["job"] = name_by_id.get(job_id, name)
            rows.append(row)

    if report_unresolved:
        for name, reason in failures.items():
            click.echo(
                f"Unresolved: {scrub_raw_ids(name)}: {scrub_raw_ids(reason)}",
                err=True,
            )
    return sorted(rows, key=event_sort_key)


@click.command("events")
@click.argument("jobs", metavar="NAME...", nargs=-1, required=True)
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
    "instance_selectors",
    multiple=True,
    metavar="RANK",
    help=(
        "Narrow to one instance, named by the Name column of `inspire job "
        "instances` — `rank=0`, or just `0`. A role name selects every "
        "instance in it. Repeat for several. Default: controller events plus "
        "every instance."
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
    jobs: tuple[str, ...],
    workspace: Optional[str],
    pick: Optional[int],
    type_filter: Optional[str],
    reason_filter: Optional[str],
    instance_selectors: tuple[str, ...],
    workload_level: bool,
    tail: int,
    follow: bool,
    interval: int,
) -> None:
    """Show events for one or more training jobs.

    For a single job, controller events and every instance's pod events are
    merged into one timeline: the controller says why the job was not created,
    the pods say why they were not scheduled or started, and they are disjoint
    sets. Use ``--instance`` to narrow to one instance, or ``--workload-level``
    to keep only the controller's half.

    Several names are answered with one batched request per 20 jobs, and the
    result is controller-level only -- per-pod events need an instance listing
    per job, so they are a single-job query. Each row is labelled with the job
    it came from.

    \b
    Examples:
      inspire job events train-a --workspace 分布式训练空间
      inspire --json job events train-a --workspace 分布式训练空间
      inspire job events train-a --workspace 分布式训练空间 --type Warning
      inspire job events train-a --workspace 分布式训练空间 --reason Unschedulable
      inspire job events train-a --workspace 分布式训练空间 --instance rank=0
      inspire job events train-a --workspace 分布式训练空间 --workload-level
      inspire job events train-a --workspace 分布式训练空间 --follow
      inspire job events train-a train-b train-c --workspace 分布式训练空间
    """
    jobs = tuple(_reject_web_job_name_at_boundary(ctx, name) for name in jobs)
    if workload_level and instance_selectors:
        _handle_error(
            ctx,
            "InvalidUsage",
            "--workload-level and --instance cannot be used together.",
            EXIT_VALIDATION_ERROR,
        )
        return
    for value in instance_selectors:
        _reject_job_instance_name(ctx, value)
    if len(jobs) > 1 and instance_selectors:
        _handle_error(
            ctx,
            "InvalidUsage",
            "--instance selects pods within one job; pass a single NAME to use it.",
            EXIT_VALIDATION_ERROR,
        )
        return
    if len(jobs) > 1 and pick is not None:
        _handle_error(
            ctx,
            "ValidationError",
            "--pick disambiguates a single name; pass one NAME to use it.",
            EXIT_VALIDATION_ERROR,
        )
        return
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return

    if len(jobs) > 1:
        reported = False

        def _fetch_batch_events() -> list[dict]:
            nonlocal reported
            try:
                rows = _batch_workload_events(
                    jobs,
                    workspace=workspace,
                    report_unresolved=not reported,
                )
                reported = True
                return rows
            finally:
                _close_web_client()

        run_events_command(
            ctx,
            fetch=_fetch_batch_events,
            type_filter=type_filter,
            reason_filter=reason_filter,
            tail=tail,
            follow=follow,
            interval=interval,
        )
        return

    job = jobs[0]

    def _fetch_web_events() -> list[dict]:
        try:
            def _fetch(resolved_id: str, session) -> list[dict]:  # noqa: ANN001
                if workload_level:
                    return sorted(
                        list_job_events(resolved_id, session=session),
                        key=event_sort_key,
                    )
                views = select_job_instance_views(
                    job_instance_views(
                        _list_all_job_instances(resolved_id, session=session)
                    ),
                    instance_selectors,
                )
                instance_events = _labelled_instance_events(
                    list_job_instance_events(
                        resolved_id,
                        [view.handle for view in views],
                        session=session,
                    ),
                    views,
                )
                if instance_selectors:
                    return sorted(instance_events, key=event_sort_key)
                merged = (
                    list_job_events(resolved_id, session=session) + instance_events
                )
                return sorted(merged, key=event_sort_key)

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
            except JobInstanceSelectionError as e:
                _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
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
