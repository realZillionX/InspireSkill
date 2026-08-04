"""`inspire notebook lifecycle <name>` — coarse run-cycle timeline.

Each row is one start to stop cycle. This complements
``inspire notebook events <name>``, which shows the fine-grained lifecycle
messages for scheduling, image pulls, preemption, container start, and image
save phases. The ongoing run has an empty end time.
"""

from __future__ import annotations

from datetime import datetime, timezone

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.auth import AuthenticationError
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.platform.web.browser_api.notebooks import list_notebook_runs

from .public_output import public_runs


def _format_duration(start: str, end: str) -> str:
    """Return a short human string like `2h 14m` or `-` if unparseable."""
    if not start or not end:
        return "-"
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        s = datetime.strptime(start, fmt).replace(tzinfo=timezone.utc)
        e = datetime.strptime(end, fmt).replace(tzinfo=timezone.utc)
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
@click.argument("name")
@pass_context
def lifecycle(ctx: Context, name: str) -> None:
    """Show the run-cycle timeline for a notebook instance.

    Each row is one start → stop cycle (restarts after auto-recycle or
    manual stop make a new row). The ongoing run has no end time.

    \b
    Examples:
      inspire notebook lifecycle <name>
      inspire --json notebook lifecycle <name>
    """
    from inspire.cli.commands.notebook.notebook_metrics import _notebook_name_to_id

    notebook_id = _notebook_name_to_id(ctx, name)
    try:
        runs = list_notebook_runs(notebook_id)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
        return
    except Exception as e:  # noqa: BLE001 — CLI boundary
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {"name": name, "runs": public_runs(runs)}
            )
        )
        return

    if not runs:
        click.echo(
            f"No run records for notebook {name} "
            "(may be newly-created or already GC'd)."
        )
        return

    runs_sorted = sorted(runs, key=lambda r: r.get("index", 0))
    header = f"{'#':>3}  {'Start':<19}  {'End':<19}  {'Duration':<9}"
    click.echo(header)
    for r in runs_sorted:
        idx = r.get("index", "?")
        # Platform may drift the field types; coerce to str defensively so
        # slicing / `_format_duration` never trip on int / None / dict.
        start_raw = str(r.get("start_time") or "")
        end_raw = str(r.get("end_time") or "")
        start = start_raw[:19] or "-"
        end_display = end_raw or "ongoing"
        dur = _format_duration(start_raw, end_raw) if end_raw else "running"
        click.echo(f"{str(idx):>3}  {start:<19}  {end_display:<19}  {dur:<9}")
