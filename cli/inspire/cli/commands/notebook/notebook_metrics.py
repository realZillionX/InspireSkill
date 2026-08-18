"""`inspire notebook metrics <name>` — notebook resource utilization.

Two questions, two Actions. The default is history: `GetTaskMetric` over an
explicit window and interval, shared with job / hpc / ray / serving. `--now`
answers the narrower "is this notebook using its card *right now*" with
`GetRealtimeNotebookMetric`, which takes no window, no interval and no
compute-group handle, and exists only on the notebook route.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Optional, cast

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import reject_id_at_boundary
from inspire.cli.utils.metrics_shared import ResolvedMetricsTarget, build_metrics_command
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import ConfigError
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, WebSession


def _notebook_lcg_from_detail(detail: object) -> Optional[str]:
    """Pull the compute-group handle from one notebook detail payload."""
    if not isinstance(detail, dict):
        return None
    start_cfg = detail.get("start_config")
    if isinstance(start_cfg, dict):
        lcg = start_cfg.get("logic_compute_group_id")
        if isinstance(lcg, str) and lcg.strip():
            return lcg.strip()
    grp = detail.get("logic_compute_group")
    if isinstance(grp, dict):
        for key in ("logic_compute_group_id", "compute_group_id"):
            value = grp.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _resolve_notebook_lcg(task_id: str, session: WebSession) -> Optional[str]:
    detail = browser_api_module.get_notebook_detail(notebook_id=task_id, session=session)
    return _notebook_lcg_from_detail(detail)


def _resolve_notebook_detail(
    ctx: Context,
    name: str,
    pick: int | None = None,
) -> tuple[dict, str, WebSession]:
    """Resolve one notebook name to its detail payload and internal handle."""
    from inspire.cli.commands.notebook import notebook_lookup as _nb
    from inspire.cli.utils.notebook_cli import WEB_AUTH_HINT, get_base_url, require_web_session
    from inspire.config.workspaces import resolve_workspace_operation_scope

    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )
    base_url = get_base_url()
    workspace_id = resolve_workspace_operation_scope(
        workspace=str(getattr(ctx, "workspace", "") or ""),
        session=session,
    )
    detail, nb_id, _workspace_id = (
        _nb._run_notebook_operation_with_stale_handle_retry(
            ctx,
            session=session,
            base_url=base_url,
            identifier=name,
            json_output=getattr(ctx, "json_output", False),
            workspace_ids=[workspace_id],
            pick=pick,
            operation=lambda notebook_id: browser_api_module.get_notebook_detail(
                notebook_id=notebook_id,
                session=session,
            ),
        )
    )
    return detail if isinstance(detail, dict) else {}, nb_id, session


def _notebook_name_to_id(
    ctx: Context,
    name: str,
    pick: int | None = None,
) -> ResolvedMetricsTarget:
    detail, nb_id, _session = _resolve_notebook_detail(ctx, name, pick)
    return ResolvedMetricsTarget(
        task_id=nb_id,
        logic_compute_group_id=_notebook_lcg_from_detail(detail),
    )


# ---------------------------------------------------------------------------
# --now: instantaneous snapshot
# ---------------------------------------------------------------------------


def _format_amount(value: float, unit: str) -> str:
    """Render one utilization number without scientific notation."""
    text = f"{int(value)}" if float(value).is_integer() else f"{value:.2f}"
    return f"{text} {unit}" if unit else text


def _format_realtime_table(rows: list[Any]) -> str:
    table_rows = [
        (
            row.resource,
            _format_amount(row.used, row.unit),
            _format_amount(row.total, row.unit),
            f"{row.usage_rate * 100:.1f}%" if row.total > 0 else "-",
        )
        for row in rows
    ]
    headers = ("Resource", "Used", "Total", "Usage")
    widths = [
        column_width(headers[index], [row[index] for row in table_rows], max_width=24)
        for index in range(len(headers))
    ]
    return "\n".join(render_table(headers, table_rows, widths, line_char="─"))


@pass_context
def _realtime_metrics(
    ctx: Context,
    *,
    name: str,
    workspace: str,
    pick: Optional[int],
) -> None:
    """Print one notebook's current utilization instead of a time series."""
    setattr(ctx, "workspace", workspace)
    name = reject_id_at_boundary(
        ctx,
        name,
        resource_type="notebook",
        list_command="inspire notebook list --workspace <workspace>",
    )

    try:
        detail, notebook_id, session = _resolve_notebook_detail(ctx, name, pick)
        rows = browser_api_module.get_notebook_realtime_metrics(
            notebook_id=notebook_id,
            session=session,
        )
    except ConfigError as exc:
        _handle_error(ctx, "ConfigError", scrub_raw_ids(exc), EXIT_CONFIG_ERROR)
        return
    except SessionExpiredError as exc:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(exc), EXIT_AUTH_ERROR)
        return
    except ValueError as exc:
        _handle_error(ctx, "APIError", scrub_raw_ids(exc), EXIT_API_ERROR)
        return

    status = str(detail.get("status") or "").upper() or "UNKNOWN"
    safe_name = scrub_raw_ids(name)

    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "resource": "notebook",
                    "name": safe_name,
                    "mode": "realtime",
                    "status": status,
                    "usage": [
                        {
                            "resource": row.resource,
                            "used": row.used,
                            "total": row.total,
                            "available": row.available,
                            "usage_rate": row.usage_rate,
                            "unit": row.unit,
                        }
                        for row in rows
                    ],
                }
            )
        )
        return

    lines = [f"Notebook Metrics — {safe_name} (now)", f"Status: {status}"]
    if status != "RUNNING":
        # Every row reads 0 on a notebook that is not running. Without this
        # line that is indistinguishable from a running but idle notebook.
        lines.append("Not running — the platform reports zero for every resource.")
    lines.append("")
    lines.append(_format_realtime_table(rows) if rows else "No resource rows returned.")
    click.echo("\n".join(lines))


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


notebook_metrics = build_metrics_command(
    resource_name="notebook",
    resource_label="Notebook",
    name_resolver=_notebook_name_to_id,
    lcg_resolver=_resolve_notebook_lcg,
)

_history_callback = cast(Callable[..., None], notebook_metrics.callback)


def _dispatch_metrics(**params: Any) -> None:
    """Route to the realtime snapshot or the shared time-series command."""
    if params.pop("now", False):
        _realtime_metrics(
            name=params["name"],
            workspace=params["workspace"],
            pick=params["pick"],
        )
        return
    _history_callback(**params)


notebook_metrics.callback = _dispatch_metrics
notebook_metrics.params.append(
    click.Option(
        ["--now"],
        is_flag=True,
        help=(
            "Report current CPU / memory / GPU / GPU-memory usage instead of a "
            "time series. Ignores --metric, --window, --start, --end, --interval, "
            "--group and the chart flags."
        ),
    )
)
# The shared factory docstring is indented for its nesting level; dedent it
# before appending, or Click's cleandoc keeps that indent on every shared line.
notebook_metrics.help = inspect.cleandoc(notebook_metrics.help or "") + (
    "\n\nPass --now for an instantaneous snapshot of the notebook's current "
    "CPU / memory / GPU / GPU-memory usage. Rates are shown as percentages; a "
    "notebook that is not RUNNING reports zero for every resource."
)


__all__ = ["notebook_metrics"]
