"""`inspire resources policy` — what the workspace does to work left running.

`<workload> quota` answers "am I allowed to take this capacity"; this answers
"how long do I get to keep it". Every workspace declares, per workload, an idle
reclaim rule and a runtime cap, and until now nothing in the CLI surfaced them:
a notebook reclaimed overnight, a training job killed at the ten-day mark and a
serving stopped for low GPU use all looked like unexplained disappearances.

The rows are the platform's own declaration, not a heuristic. Read them before
leaving anything running unattended, and before assuming a long job will
survive the weekend.
"""

from __future__ import annotations

from typing import Any, Optional

import click

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
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.resource_index import QUOTA_WORKLOADS
from inspire.config import Config, ConfigError
from inspire.config.workspaces import (
    resolve_workspace_operation_scope,
    workspace_name_map,
)
from inspire.platform.web.browser_api.schedule_config import (
    WorkloadSchedulePolicy,
    format_duration,
    get_workspace_schedule_policy,
)
from inspire.platform.web.session import (
    AuthenticationError,
    SessionExpiredError,
    get_web_session,
)

# The platform applies the serving reclaim rule to custom deployments only;
# the console says so next to the switch.
_SERVING_SCOPE_NOTE = (
    "serving: the reclaim rule applies to custom deployments only."
)


def _public_row(
    policy: WorkloadSchedulePolicy,
    *,
    workspace: str,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "workspace": workspace,
        "workload": policy.workload,
        "configured": policy.configured,
    }
    if not policy.configured:
        return row

    row["auto_reclaim"] = bool(policy.auto_reclaim)
    reclaim = policy.reclaim_description
    if reclaim:
        row["reclaim_rule"] = reclaim
    if policy.max_runtime_minutes:
        row["max_runtime_minutes"] = policy.max_runtime_minutes
        row["max_runtime"] = format_duration(policy.max_runtime_minutes)
    if policy.daily_shutdown:
        row["daily_shutdown"] = policy.daily_shutdown
    if policy.applies_to:
        row["applies_to"] = policy.applies_to
    # Whether the platform saves the environment before it reclaims. Kept out
    # of the table because only the notebook row ever carries it, but kept in
    # `--json` because "will I get my work back" is the whole question this
    # command exists to answer.
    if policy.auto_save is not None:
        row["auto_save"] = policy.auto_save
    return row


def _time_limit(row: dict[str, Any]) -> str:
    if not row.get("configured"):
        return "-"
    limits = []
    if row.get("max_runtime"):
        limits.append(f"max {row['max_runtime']}")
    if row.get("daily_shutdown"):
        limits.append(f"daily {row['daily_shutdown']}")
    return ", ".join(limits) if limits else "none"


def _reclaim(row: dict[str, Any]) -> str:
    if not row.get("configured"):
        return "-"
    return "on" if row.get("auto_reclaim") else "off"


def _format_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No scheduling policy reported."

    headers = ("Workload", "Reclaim", "Idle Rule", "Time Limit")

    table_rows: list[tuple[str, ...]] = [
        (
            str(row.get("workload") or "-"),
            _reclaim(row),
            str(row.get("reclaim_rule") or ("-" if not row.get("configured") else "none")),
            _time_limit(row),
        )
        for row in rows
    ]

    widths = [
        column_width(header, [row[index] for row in table_rows], max_width=44)
        for index, header in enumerate(headers)
    ]
    return "\n".join(render_table(headers, table_rows, widths, line_char="─"))


def _notes(rows: list[dict[str, Any]]) -> list[str]:
    notes: list[str] = []
    unconfigured = sorted(
        {str(row["workload"]) for row in rows if not row.get("configured")}
    )
    if unconfigured:
        notes.append(
            f"{', '.join(unconfigured)}: this workspace declares no scheduling "
            "policy — not the same as no limits."
        )
    if any(row.get("workload") == "serving" and row.get("auto_reclaim") for row in rows):
        notes.append(_SERVING_SCOPE_NOTE)
    return notes


@click.command("policy")
@click.option(
    "--workspace",
    required=True,
    metavar="NAME",
    help="Workspace name.",
)
@click.option(
    "--workload",
    type=click.Choice(sorted(QUOTA_WORKLOADS), case_sensitive=False),
    default=None,
    help="Show one workload only (default: all five).",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum rows to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every row.")
@pass_context
def policy_resources(
    ctx: Context,
    workspace: str,
    workload: Optional[str],
    limit: Optional[int],
    show_all: bool,
) -> None:
    """Show a workspace's idle-reclaim rules and runtime caps per workload.

    `Reclaim` is whether the scheduler takes the workload back on its own,
    `Idle Rule` is the condition that triggers it, and `Time Limit` is the hard
    cap — a `max` duration for jobs, a `daily` wall-clock shutdown for
    notebooks. A dash means the workspace declares no policy for that workload,
    which is not the same as declaring no limits.

    Use `inspire <workload> quota` for legal resource shapes, `inspire
    resources availability` for guarantee-level capacity, and `inspire
    resources nodes` for physical whole-node availability.

    \b
    Examples:
        inspire resources policy --workspace 分布式训练空间
        inspire resources policy --workspace CPU资源空间 --workload notebook
        inspire --json resources policy --workspace 分布式训练空间 --all
    """
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    selected = workload.lower() if workload else None
    try:
        Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = resolve_workspace_operation_scope(
            workspace=workspace,
            session=session,
        )
        label = scrub_raw_ids(workspace_name_map(session).get(workspace_id) or workspace)

        rows: list[dict[str, Any]] = []
        for policy in get_workspace_schedule_policy(workspace_id, session=session):
            row = _public_row(policy, workspace=label)
            if selected and row["workload"] != selected:
                continue
            rows.append(row)

        page = bound_collection(rows, limit=effective_limit)

        if ctx.json_output:
            click.echo(
                json_formatter.format_json({"items": page.items, **page.metadata()})
            )
            return

        click.echo(_format_rows(page.items))
        for note in _notes(page.items):
            click.echo(note)
        notice = truncation_notice(page, full_option="--all")
        if notice:
            click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except (AuthenticationError, SessionExpiredError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


__all__ = ["policy_resources"]
