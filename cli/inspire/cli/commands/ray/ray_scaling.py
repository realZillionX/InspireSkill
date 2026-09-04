"""`inspire ray scaling <name>` — elastic worker-group replica history.

Elastic worker groups are the reason a workload belongs in `ray` rather than
`job` or `hpc`, and until now nothing in the CLI could answer the question that
follows from submitting one: did the ``min``/``max`` range a group was created
with ever actually move? `ray status` shows the range that was *requested*;
this shows what the platform did with it.

It is a subcommand rather than a section of `ray status` for two reasons. It is
a collection — one row per replica change, growing over the cluster's life —
so it needs the `--limit` / `--all` budget that every collection command in
this CLI carries, and folding a paged collection into a flat detail view would
either break that budget or push paging flags onto `status`. And it matches how
this command group already splits observation: `events`, `instances` and
`metrics` are each their own subcommand over one resolved job.
"""

from __future__ import annotations

from typing import Any, Optional

import click

from inspire.cli.commands.ray.ray_commands import (
    _reject_ray_name_at_boundary,
    _run_readonly_ray_operation,
)
from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.collection_output import (
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import NAME_PICK_HELP
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.platform.web.browser_api.ray_jobs import list_ray_job_scaling_histories
from inspire.platform.web.session import SessionExpiredError, get_web_session

_NAME_RESOLUTION_LIMIT = 10000

# The Action declares no sorter, so ordering is done here. Asking for every row
# is what the console does and is what makes that ordering safe: a bounded page
# of an unordered result set cannot be assumed to hold the most recent changes.
_SCALING_FETCH_ALL = -1


def _int_or_none(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _event_time(item: dict[str, Any]) -> int:
    for key in ("event_time", "created_at", "timestamp_ms"):
        value = _int_or_none(item.get(key))
        if value is not None:
            return value
    return 0


def _text(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value in (None, "") or isinstance(value, (dict, list, tuple, set)):
            continue
        text = scrub_raw_ids(value).strip()
        if text and "<redacted>" not in text:
            return text
    return ""


def _public_ray_scaling_events(
    items: list[dict[str, Any]],
    *,
    group: str = "",
) -> list[dict[str, Any]]:
    """Project scaling rows onto a stable allowlist.

    The wire row is ``event_time`` / ``event_type`` / ``replicas_before`` /
    ``replicas_after``. ``event_type`` is one of ``initialized``, ``scale_up``
    and ``scale_down``; it is passed through rather than translated so the JSON
    stays greppable across locales.
    """
    projected: list[dict[str, Any]] = []
    for item in items:
        row: dict[str, Any] = {
            "time": human_formatter.format_epoch(_event_time(item)),
            "event": _text(item, "event_type", "type") or "unknown",
        }
        group_name = _text(item, "worker_group_name", "group_name") or scrub_raw_ids(
            group
        ).strip()
        if group_name:
            row["group"] = group_name
        before = _int_or_none(item.get("replicas_before"))
        after = _int_or_none(item.get("replicas_after"))
        if before is not None:
            row["replicas_before"] = before
        if after is not None:
            row["replicas_after"] = after
        projected.append(row)
    return projected


def _format_ray_scaling(events: list[dict[str, Any]]) -> str:
    if not events:
        return "No Ray scaling history found."

    columns = [("time", "Time")]
    if any(event.get("group") for event in events):
        columns.append(("group", "Group"))
    columns.append(("event", "Event"))
    columns.append(("replicas", "Replicas"))

    rows: list[tuple[str, ...]] = []
    for event in events:
        before = event.get("replicas_before")
        after = event.get("replicas_after")
        if before is None and after is None:
            replicas = "-"
        else:
            replicas = f"{'-' if before is None else before} -> {'-' if after is None else after}"
        rows.append(
            tuple(
                replicas if key == "replicas" else str(event.get(key, "-") or "-")
                for key, _ in columns
            )
        )

    widths = [
        column_width(label, [row[index] for row in rows], max_width=48)
        for index, (_, label) in enumerate(columns)
    ]
    rendered = render_table(
        tuple(label for _, label in columns),
        rows,
        widths,
    )
    return "\n".join(rendered)


@click.command("scaling")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@click.option(
    "--group",
    default=None,
    metavar="NAME",
    help=(
        "Show only this worker group, named as it appears in "
        "`inspire ray status` and the Role column of `inspire ray instances`. "
        "Default: every group."
    ),
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum changes to display; the most recent are kept (default: 20).",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    help="Show the complete scaling history.",
)
@pass_context
def scaling_ray(
    ctx: Context,
    name: str,
    workspace: str,
    pick: Optional[int],
    group: Optional[str],
    limit: Optional[int],
    show_all: bool,
) -> None:
    """Show elastic worker-group replica changes for a Ray job.

    Each row is one replica-count change the platform made to a worker group:
    the `initialized` entry the group started from, then every `scale_up` /
    `scale_down` after it. An empty history means the elastic range was never
    exercised — the group ran at the replica count it started with.

    Rows are printed oldest-first; when the history is longer than the budget
    the most recent changes are the ones kept.

    \b
    Examples:
        inspire ray scaling pipeline --workspace CPU资源空间
        inspire ray scaling pipeline --workspace CPU资源空间 --group decode
        inspire ray scaling pipeline --workspace CPU资源空间 --all
        inspire --json ray scaling pipeline --workspace CPU资源空间
    """
    try:
        output_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    name = _reject_ray_name_at_boundary(ctx, name)
    group_filter = (group or "").strip()

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        items, total = _run_readonly_ray_operation(
            ctx,
            session=session,
            name=name,
            workspace=workspace,
            limit=_NAME_RESOLUTION_LIMIT,
            pick=pick,
            operation=lambda ray_job_id, live_session: list_ray_job_scaling_histories(
                ray_job_id,
                worker_group_name=group_filter,
                page_num=1,
                page_size=_SCALING_FETCH_ALL,
                session=live_session,
            ),
        )

        ordered = sorted(items, key=_event_time)
        recent = ordered if output_limit is None else ordered[-output_limit:]
        page = bound_collection(recent, limit=output_limit, total=total)
        public_items = _public_ray_scaling_events(page.items, group=group_filter)

        if ctx.json_output:
            payload: dict[str, Any] = {
                "name": scrub_raw_ids(name),
                "items": public_items,
                **page.metadata(),
            }
            click.echo(json_formatter.format_json(payload))
            return

        click.echo(_format_ray_scaling(public_items))
        notice = truncation_notice(page)
        if notice:
            click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = ["scaling_ray"]
