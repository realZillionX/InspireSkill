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
from inspire.config.workspaces import resolve_workspace_query_scope
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api import NodeSpec
from inspire.platform.web.session import SessionExpiredError, get_web_session

_REDACTED_ID_RE = re.compile(
    r"(?:\b[A-Za-z][A-Za-z0-9_-]*-)?(?:<redacted>|<[^<>]+-id>)"
)


def _display_name(value: object, *, fallback: str = "-") -> str:
    text = _REDACTED_ID_RE.sub(" ", scrub_raw_ids(value))
    return " ".join(text.split()) or fallback


def _resolve_workspace_scope(
    *,
    config: Optional[Config],
    session,
    workspace: Optional[str],
) -> tuple[Optional[str], bool]:
    if config is None:
        raise ConfigError("Workspace selection requires a loaded config.")
    workspace_ids, all_workspaces = resolve_workspace_query_scope(
        workspace=workspace,
        session=session,
    )
    return (None if all_workspaces else workspace_ids[0], all_workspaces)


def _public_group(row: dict) -> dict[str, object]:
    return {
        "compute_group": _display_name(row.get("group_name")),
        "workspace": _display_name(row.get("workspace_name"), fallback=""),
        "gpus_per_node": row["gpu_per_node"],
        "total_nodes": row["total_nodes"],
        "ready_nodes": row["ready_nodes"],
        "full_free_nodes": row["full_free_nodes"],
        "full_free_gpus": row["full_free_gpus"],
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


@click.command("nodes")
@click.option(
    "--workspace",
    required=True,
    metavar="NAME|all",
    help="Workspace name or 'all'.",
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
        "Only show groups with at least N fully idle 8-GPU nodes. "
        "Use before multi-node jobs that need whole nodes, not scattered GPUs."
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
    """Show how many whole 8-GPU nodes are currently free per compute group.

    This accounts for GPU fragmentation across nodes, so it is the right view
    when a workload needs whole nodes instead of scattered free GPUs.

    \b
    `Node Spec` is the largest single node the group can schedule onto, which
    is the ceiling a `--quota gpu,cpu,mem` triple has to fit under. Groups with
    mixed hardware report every distinct shape under `--json`.

    \b
    Examples:
        inspire resources nodes --workspace 分布式训练空间
        inspire resources nodes --workspace all --group H200
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
        config = None
        try:
            config, _ = Config.from_files_and_env(
                require_credentials=False
            )
        except Exception:
            config = None
        session = get_web_session()
        workspace_id, all_workspaces = _resolve_workspace_scope(
            config=config,
            session=session,
            workspace=workspace,
        )
        workspace_names = dict(session.all_workspace_names or {})

        accurate_availability = browser_api_module.get_accurate_resource_availability(
            workspace_id=workspace_id,
            session=session,
            include_cpu=False,
            all_workspaces=all_workspaces,
        )
        accurate_map = {a.group_id: a.available_gpus for a in accurate_availability}
        name_map = {a.group_id: a.group_name for a in accurate_availability}
        workspace_map = {
            a.group_id: a.workspace_name or workspace_names.get(a.workspace_id, "")
            for a in accurate_availability
        }

        group_ids = [a.group_id for a in accurate_availability]
        workspace_id_map = {a.group_id: a.workspace_id for a in accurate_availability}
        counts = browser_api_module.get_full_free_node_counts(
            group_ids,
            gpu_per_node=8,
            workspace_id_by_group=workspace_id_map,
            session=session,
        )

        # Fill missing names and apply filter
        filtered: list[dict] = []
        group_lower = (group or "").lower()
        for c in counts:
            name = c.group_name or name_map.get(c.group_id, "") or "Unknown"
            if group_lower and group_lower not in name.lower():
                continue
            if c.full_free_nodes < min_nodes:
                continue
            # Use accurate available GPUs if available, otherwise fall back to computed
            free_gpus = accurate_map.get(c.group_id, c.full_free_nodes * c.gpu_per_node)
            # Free-node counts say how much is idle but never what the idle
            # hardware is, so a `--quota gpu,cpu,mem` triple could not be
            # checked against the group it would be submitted to.
            specs = browser_api_module.list_node_specs(
                workspace_id_map.get(c.group_id, ""),
                logic_compute_group_id=c.group_id,
                session=session,
            )
            filtered.append(
                {
                    "group_id": c.group_id,
                    "group_name": name,
                    "workspace_name": workspace_map.get(c.group_id, ""),
                    "gpu_per_node": c.gpu_per_node,
                    "total_nodes": c.total_nodes,
                    "ready_nodes": c.ready_nodes,
                    "full_free_nodes": c.full_free_nodes,
                    "full_free_gpus": free_gpus,
                    "node_specs": [_public_spec(spec) for spec in specs],
                    "node_spec_label": specs[0].label if specs else "",
                }
            )

        # Sort by full_free_nodes descending
        filtered.sort(
            key=lambda x: (
                x["full_free_nodes"],
                x["full_free_gpus"],
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

        show_workspace = (
            len({row["workspace_name"] for row in shown_rows if row["workspace_name"]}) > 1
        )
        if show_workspace:
            headers: tuple[str, ...] = (
                "Workspace",
                "Group",
                "Node Spec",
                "Full Free",
                "Ready",
                "Total",
                "Free GPUs",
            )
            widths = [16, 25, 22, 10, 8, 8, 10]
            aligns = ["left", "left", "left", "right", "right", "right", "right"]
        else:
            headers = ("Group", "Node Spec", "Full Free", "Ready", "Total", "Free GPUs")
            widths = [25, 22, 10, 8, 8, 10]
            aligns = ["left", "left", "right", "right", "right", "right"]

        table_rows: list[tuple[object, ...]] = []
        for row in shown_rows:
            name = _display_name(row["group_name"])
            spec = row["node_spec_label"] or "-"
            full_free = row["full_free_nodes"]
            ready = row["ready_nodes"]
            total = row["total_nodes"]
            free_gpus = row["full_free_gpus"]

            if show_workspace:
                table_rows.append(
                    (
                        _display_name(row["workspace_name"], fallback=""),
                        name,
                        spec,
                        full_free,
                        ready,
                        total,
                        free_gpus,
                    )
                )
            else:
                table_rows.append((name, spec, full_free, ready, total, free_gpus))
        click.echo(
            "\n".join(render_table(headers, table_rows, widths, aligns=aligns, line_char="─"))
        )
        notice = truncation_notice(page)
        if notice:
            click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
