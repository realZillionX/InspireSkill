"""Resources nodes command (full free nodes per group)."""

from __future__ import annotations

import re
from typing import Optional

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
from inspire.cli.formatters.table import render_table
from inspire.cli.utils.collection_output import (
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import reject_id_at_boundary
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.config.workspaces import resolve_workspace_operation_scope
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api import NodeSpec
from inspire.platform.web.session import (
    AuthenticationError,
    SessionExpiredError,
    get_web_session,
)

_REDACTED_ID_RE = re.compile(
    r"(?:\b[A-Za-z][A-Za-z0-9_-]*-)?(?:<redacted>|<[^<>]+-id>)"
)


def _display_name(value: object, *, fallback: str = "-") -> str:
    text = _REDACTED_ID_RE.sub(" ", scrub_raw_ids(value))
    return " ".join(text.split()) or fallback


def _resolve_workspace_scope(
    *,
    session,
    workspace: Optional[str],
) -> str:
    return resolve_workspace_operation_scope(workspace=workspace, session=session)


def _public_group(row: dict) -> dict[str, object]:
    return {
        "compute_group": _display_name(row.get("group_name")),
        "workspace": _display_name(row.get("workspace_name"), fallback=""),
        "gpus_per_node": row["gpu_per_node"],
        "total_nodes": row["total_nodes"],
        "ready_nodes": row["ready_nodes"],
        "full_free_nodes": row["full_free_nodes"],
        "reclaimable_nodes": row["reclaimable_nodes"],
        "high_priority_free_nodes": row["high_priority_free_nodes"],
        "full_free_gpus": row["full_free_gpus"],
        "high_priority_free_gpus": row["high_priority_free_gpus"],
        "node_specs": row["node_specs"],
    }


def _public_spec(spec: NodeSpec) -> dict[str, object]:
    return {
        "node_type": spec.node_type,
        "gpu_type": spec.gpu_type,
        "gpu_count": spec.gpu_count,
        "cpu_count": spec.cpu_count,
        "memory_gib": spec.memory_gib,
        "job_types": list(spec.job_types),
    }


@click.command(
    "nodes",
    hidden=True,
)
@click.option(
    "--workspace",
    required=True,
    metavar="NAME",
    help="Workspace name.",
)
@click.option(
    "--group",
    metavar="NAME",
    help=(
        "Filter by compute group name keyword/substring; "
        "full name is not required."
    ),
)
@click.option(
    "--min-nodes",
    type=click.IntRange(min=0),
    default=0,
    show_default=True,
    help=(
        "Only show groups with at least N whole 8-GPU nodes available to a "
        "high-priority job after low-priority preemption."
    ),
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum compute groups to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every matching compute group.")
@pass_context
def list_nodes(
    ctx: Context,
    group: str,
    min_nodes: int,
    workspace: str,
    limit: int | None,
    show_all: bool,
) -> None:
    """Compatibility alias for the node columns in `resources availability`.

    `Free Now` counts schedulable nodes with no task or GPU allocation. `High
    Pri` also counts nodes whose every occupant was submitted at a preemptible
    priority. Unknown or mixed-priority occupants are never treated as free.
    `Idle GPUs` is exactly `Free Now * 8`, so quota overcommit cannot make it
    negative. Use `resources availability` for guarantee-level GPU capacity.
    This view is for whole-node placement, not scattered GPUs.

    `Node Spec` is the largest single node the group can schedule onto, which
    is the ceiling a `--quota gpu,cpu,mem` triple has to fit under. Groups with
    mixed hardware report every distinct shape under `--json`.

    \b
    Examples:
        inspire resources nodes --workspace 分布式训练空间
        inspire resources nodes --workspace 分布式训练空间 --group H200
        inspire resources nodes --workspace 分布式训练空间 --min-nodes 2
    """
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    try:
        if group:
            group = reject_id_at_boundary(
                ctx,
                group,
                resource_type="compute group",
                list_command=f"inspire resources nodes --workspace {workspace}",
            )
        Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = _resolve_workspace_scope(
            session=session,
            workspace=workspace,
        )
        workspace_names = dict(session.all_workspace_names or {})

        inventory = browser_api_module.get_resource_inventory(
            workspace_id=workspace_id,
            session=session,
            include_cpu=False,
        )
        name_map = {a.group_id: a.group_name for a in inventory}
        workspace_map = {
            a.group_id: a.workspace_name or workspace_names.get(a.workspace_id, "")
            for a in inventory
        }

        # Fill missing names and apply filter
        filtered: list[dict] = []
        group_lower = (group or "").lower()
        for resource in inventory:
            name = resource.group_name or name_map.get(resource.group_id, "") or "Unknown"
            if group_lower and group_lower not in name.lower():
                continue
            if resource.high_priority_free_nodes < min_nodes:
                continue
            specs = list(resource.node_specs)
            filtered.append(
                {
                    "group_id": resource.group_id,
                    "group_name": name,
                    "workspace_name": workspace_map.get(resource.group_id, ""),
                    "gpu_per_node": resource.gpu_per_node,
                    "total_nodes": resource.total_nodes,
                    "ready_nodes": resource.ready_nodes,
                    "full_free_nodes": resource.full_free_nodes,
                    "reclaimable_nodes": resource.reclaimable_nodes,
                    "high_priority_free_nodes": resource.high_priority_free_nodes,
                    "full_free_gpus": resource.full_free_gpus,
                    "high_priority_free_gpus": resource.high_priority_free_gpus,
                    "node_specs": [_public_spec(spec) for spec in specs],
                    "node_spec_label": specs[0].label if specs else "",
                }
            )

        # Default job priority is high, so rank by the capacity that submission
        # can actually obtain, then prefer capacity that needs no preemption.
        filtered.sort(
            key=lambda x: (
                x["high_priority_free_nodes"],
                x["full_free_nodes"],
                x["ready_nodes"],
            ),
            reverse=True,
        )
        page = bound_collection(filtered, limit=effective_limit)
        shown_rows = page.items
        public_groups = [_public_group(row) for row in shown_rows]

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "items": public_groups,
                        **page.metadata(),
                    }
                )
            )
            return

        if not filtered:
            click.echo("No compute groups match.")
            return

        headers = (
            "Group",
            "Node Spec",
            "Free Now",
            "High Pri",
            "Ready",
            "Total",
            "Idle GPUs",
        )
        widths = [25, 22, 9, 9, 7, 7, 9]
        aligns = ["left", "left", "right", "right", "right", "right", "right"]

        table_rows: list[tuple[object, ...]] = [
            (
                _display_name(row["group_name"]),
                row["node_spec_label"] or "-",
                row["full_free_nodes"],
                row["high_priority_free_nodes"],
                row["ready_nodes"],
                row["total_nodes"],
                row["full_free_gpus"],
            )
            for row in shown_rows
        ]
        click.echo(
            "\n".join(render_table(headers, table_rows, widths, aligns=aligns, line_char="─"))
        )
        notice = truncation_notice(page)
        if notice:
            click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except (AuthenticationError, SessionExpiredError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
