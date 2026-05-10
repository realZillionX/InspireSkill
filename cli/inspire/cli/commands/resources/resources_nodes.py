"""Resources nodes command (full free nodes per group)."""

from __future__ import annotations

from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.platform.web import browser_api as browser_api_module
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.platform.web.session import SessionExpiredError, get_web_session
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id
from .table import render_table


def _workspace_name_map(
    *,
    config: Optional[Config],
    session,
) -> dict[str, str]:
    del config
    return dict(session.all_workspace_names or {})


def _resolve_workspace_scope(
    *,
    config: Optional[Config],
    session,
    explicit_workspace_name: Optional[str],
    show_all: bool,
) -> tuple[Optional[str], bool]:
    if explicit_workspace_name is not None:
        if config is None:
            raise ConfigError("Workspace selection requires a loaded config.")
        return (
            select_workspace_id(
                config,
                explicit_workspace_name=explicit_workspace_name,
                session=session,
            ),
            False,
        )
    return (None, show_all)


@click.command("nodes")
@click.option("--group", help="Filter by compute group name (partial match)")
@click.option(
    "--min-full-free-nodes",
    "--min-free",
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
    "--all",
    "show_all",
    is_flag=True,
    help="Include all visible workspaces instead of only the current workspace",
)
@click.option("--workspace-name", default=None, help="Workspace name override")
@pass_context
def list_nodes(
    ctx: Context,
    group: str,
    min_full_free_nodes: int,
    show_all: bool,
    workspace_name: Optional[str],
) -> None:
    """Show how many whole 8-GPU nodes are currently free per compute group.

    This accounts for GPU fragmentation across nodes, so it is the right view
    when a workload needs whole nodes instead of scattered free GPUs.

    \b
    Examples:
        inspire resources nodes
        inspire resources nodes --group H200
        inspire resources nodes --min-nodes 2
    """
    try:
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
            explicit_workspace_name=workspace_name,
            show_all=show_all,
        )
        workspace_names = _workspace_name_map(config=config, session=session)

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
        counts = browser_api_module.get_full_free_node_counts(group_ids, gpu_per_node=8)

        # Fill missing names and apply filter
        filtered: list[dict] = []
        group_lower = (group or "").lower()
        for c in counts:
            name = c.group_name or name_map.get(c.group_id, "") or "Unknown"
            if group_lower and group_lower not in name.lower():
                continue
            if c.full_free_nodes < min_full_free_nodes:
                continue
            # Use accurate available GPUs if available, otherwise fall back to computed
            free_gpus = accurate_map.get(c.group_id, c.full_free_nodes * c.gpu_per_node)
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
        recommendation = filtered[0] if filtered else None

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "groups": filtered,
                        "recommendation": recommendation,
                        "min_full_free_nodes": min_full_free_nodes,
                        "workspace_filter": workspace_id
                        or ("all" if all_workspaces else "current"),
                        "total_full_free_nodes": sum(x["full_free_nodes"] for x in filtered),
                    }
                )
            )
            return

        show_workspace = (
            len({row["workspace_name"] for row in filtered if row["workspace_name"]}) > 1
        )
        click.echo("")
        click.echo("📊 Full-Free 8-GPU Nodes by Compute Group")
        if min_full_free_nodes:
            click.echo(f"Filter: at least {min_full_free_nodes} full-free node(s)")
        if show_workspace:
            headers: tuple[str, ...] = (
                "Workspace",
                "Group",
                "Full Free",
                "Ready",
                "Total",
                "Free GPUs",
                "",
            )
            widths = [16, 25, 10, 8, 8, 10, 2]
            aligns = ["left", "left", "right", "right", "right", "right", "left"]
        else:
            headers = ("Group", "Full Free", "Ready", "Total", "Free GPUs", "")
            widths = [25, 10, 8, 8, 10, 2]
            aligns = ["left", "right", "right", "right", "right", "left"]

        total_full_free = 0
        total_free_gpus = 0
        table_rows: list[tuple[object, ...]] = []
        for row in filtered:
            name = scrub_raw_ids(row["group_name"])
            full_free = row["full_free_nodes"]
            ready = row["ready_nodes"]
            total = row["total_nodes"]
            free_gpus = row["full_free_gpus"]

            total_full_free += full_free
            total_free_gpus += free_gpus

            if full_free >= 10:
                indicator = "🟢"
            elif full_free >= 3:
                indicator = "🟡"
            elif full_free > 0:
                indicator = "🟠"
            else:
                indicator = "🔴"

            if show_workspace:
                table_rows.append(
                    (
                        scrub_raw_ids(row["workspace_name"]),
                        name,
                        full_free,
                        ready,
                        total,
                        free_gpus,
                        indicator,
                    )
                )
            else:
                table_rows.append((name, full_free, ready, total, free_gpus, indicator))

        if show_workspace:
            table_rows.append(("TOTAL", "", total_full_free, "", "", total_free_gpus, ""))
        else:
            table_rows.append(("TOTAL", total_full_free, "", "", total_free_gpus, ""))
        click.echo(
            "\n".join(render_table(headers, table_rows, widths, aligns=aligns, line_char="─"))
        )
        if recommendation is not None:
            workspace = scrub_raw_ids(str(recommendation.get("workspace_name") or ""))
            group_name = scrub_raw_ids(str(recommendation.get("group_name") or "Unknown"))
            prefix = f"{workspace} / " if show_workspace and workspace else ""
            click.echo("")
            click.echo(
                "Recommended: "
                f"{prefix}{group_name} "
                f"({recommendation['full_free_nodes']} full-free node(s))"
            )
        click.echo("")
        click.echo("Full Free = READY nodes with 8 GPUs and no running tasks")
        click.echo("Free GPUs = Total available GPUs (matches 'inspire resources list')")
        click.echo("")

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
