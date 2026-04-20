"""`inspire notebook lifecycle <id>` — coarse run-cycle timeline.

Payload comes from Browser API `POST /api/v1/run_index/list` via
`browser_api.notebooks.list_notebook_runs`. Each entry is one
start → stop cycle: notebooks can be re-started after being auto-recycled
or manually stopped, so a long-lived instance typically accumulates 3-10
run records.

This complements `inspire notebook events <id>`, which returns the
*fine-grained* K8s-ish timeline (scheduling, image pulls, preemption,
container start, save-as-image phases). The events tab and the run-index
tab are rendered by different components on the web portal — the web
`生命周期` tab uses `/run_index/list`; our `events` command uses
`/notebook/events`.

The ongoing run has `end_time = ""`.
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
@click.argument("notebook_id")
@click.option(
    "--json",
    "json_output_local",
    is_flag=True,
    help="Output as JSON. Equivalent to top-level `--json`.",
)
@pass_context
def lifecycle(ctx: Context, notebook_id: str, json_output_local: bool) -> None:
    """Show the run-cycle timeline for a notebook instance.

    Each row is one start → stop cycle (restarts after auto-recycle or
    manual stop make a new row). The ongoing run has no end time.

    \b
    Examples:
      inspire notebook lifecycle <id>
      inspire --json notebook lifecycle <id>
    """
    try:
        runs = list_notebook_runs(notebook_id)
    except AuthenticationError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
        return
    except Exception as e:  # noqa: BLE001 — CLI boundary
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    if json_output_local or ctx.json_output:
        click.echo(
            json_formatter.format_json({"notebook_id": notebook_id, "runs": runs})
        )
        return

    if not runs:
        click.echo(
            f"No run records for notebook {notebook_id} "
            "(may be newly-created or already GC'd)."
        )
        return

    runs_sorted = sorted(runs, key=lambda r: r.get("index", 0))
    header = f"{'#':>3}  {'Start':<19}  {'End':<19}  {'Duration':<9}"
    click.echo(f"Notebook runs ({len(runs_sorted)})")
    click.echo(header)
    click.echo("-" * len(header))
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
