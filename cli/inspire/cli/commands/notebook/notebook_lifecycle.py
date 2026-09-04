"""`inspire notebook lifecycle <name> --workspace <workspace>` — coarse run-cycle timeline.

Each row is one start to stop cycle. This complements
``inspire notebook events <name>``, which shows the fine-grained lifecycle
messages for scheduling, image pulls, preemption, container start, and image
save phases. The ongoing run has an empty end time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import click

from inspire.config import ConfigError
from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.collection_output import (
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import NAME_PICK_HELP, reject_id_at_boundary
from inspire.platform.web.browser_api.notebooks import list_notebook_runs
from inspire.platform.web.session import SessionExpiredError

from .public_output import public_runs


_RUN_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# `ListRunIndex` is the one notebook Action that answers with a naive
# wall-clock string instead of an epoch. It is the platform's own clock, which
# is China Standard Time and has no DST — verified 2026-08-15 by matching a run
# whose start read `2026-08-16 01:58:03` against the same notebook's
# `Notebook is ready` event at 17:58:03Z. Every other command renders epochs in
# the machine's local time, so a run cycle printed verbatim would sit hours
# away from the events describing it.
_PLATFORM_TZ = timezone(timedelta(hours=8))


def _to_local(value: str) -> str:
    """Render one platform wall-clock string in the machine's local time."""
    text = str(value or "").strip()[:19]
    if not text:
        return ""
    try:
        stamp = datetime.strptime(text, _RUN_TIME_FORMAT)
    except ValueError:
        return str(value)
    return (
        stamp.replace(tzinfo=_PLATFORM_TZ).astimezone().strftime(_RUN_TIME_FORMAT)
    )


def _format_duration(start: str, end: str) -> str:
    """Return a short human string like `2h 14m` or `-` if unparseable."""
    if not start or not end:
        return "-"
    try:
        fmt = _RUN_TIME_FORMAT
        s = datetime.strptime(start[:19], fmt).replace(tzinfo=timezone.utc)
        e = datetime.strptime(end[:19], fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        return "-"
    secs = int((e - s).total_seconds())
    if secs < 0:
        return "-"
    h, rem = divmod(secs, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


@click.command("lifecycle")
@click.argument("name", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum run cycles to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every run cycle.")
@pass_context
def lifecycle(
    ctx: Context,
    name: str,
    workspace: str,
    pick: int | None,
    limit: int | None,
    show_all: bool,
) -> None:
    """Show the run-cycle timeline for a notebook instance.

    Each row is one start → stop cycle (restarts after auto-recycle or
    manual stop make a new row). The ongoing run has no end time.

    \b
    Examples:
      inspire notebook lifecycle <name> --workspace <workspace>
      inspire notebook lifecycle <name> --workspace <workspace> --pick 2
      inspire notebook lifecycle <name> --workspace <workspace> --limit 10
      inspire --json notebook lifecycle <name> --workspace <workspace>
    """
    try:
        output_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="notebook",
        list_command="inspire notebook list --workspace <workspace|all>",
    )
    setattr(ctx, "workspace", workspace)
    try:
        from inspire.cli.commands.notebook.notebook_metrics import _notebook_name_to_id

        target = _notebook_name_to_id(ctx, name, pick=pick)
        runs = list_notebook_runs(target.task_id)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
        return
    except Exception as e:  # noqa: BLE001 — CLI boundary
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    if not runs:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"items": []},
                )
            )
        else:
            click.echo(
                f"No run records for notebook {name} "
                "(may be newly-created or already GC'd)."
            )
        return

    runs_sorted = sorted(runs, key=lambda r: r.get("index", 0))
    visible_runs = (
        runs_sorted if output_limit is None else runs_sorted[-output_limit:]
    )
    page = bound_collection(
        visible_runs,
        limit=None,
        total=len(runs_sorted),
    )

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "items": public_runs(page.items),
                    **page.metadata(),
                }
            )
        )
        return

    table_rows: list[tuple[str, str, str, str]] = []
    for r in page.items:
        idx = r.get("index", "?")
        # Platform may drift the field types; coerce to str defensively so
        # slicing / `_format_duration` never trip on int / None / dict.
        start_raw = str(r.get("start_time") or "")
        end_raw = str(r.get("end_time") or "")
        start = _to_local(start_raw) or "-"
        end_display = _to_local(end_raw) or "ongoing"
        dur = _format_duration(start_raw, end_raw) if end_raw else "running"
        table_rows.append((str(idx), start, end_display, dur))
    widths = [
        column_width(header, [row[index] for row in table_rows], max_width=max_width)
        for index, (header, max_width) in enumerate(
            (("#", 3), ("Start", 19), ("End", 19), ("Duration", 9))
        )
    ]
    click.echo(
        "\n".join(
            render_table(
                ("#", "Start", "End", "Duration"),
                table_rows,
                widths,
                aligns=("right", "left", "left", "left"),
            )
        )
    )
    notice = truncation_notice(page)
    if notice:
        click.echo(notice)
