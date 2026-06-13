"""``inspire config context`` — name-first view of the active account.

Structured pieces of the loaded config (active account, projects,
workspaces, compute groups) aren't reachable through ``inspire config
show``, which is focused on the flat env-var-backed options. This command
    fills that gap with a **name-only** view: every workspace, project, and
    compute group is shown by its platform name (``CI-情境智能``,
    ``H200-3号机房``), not by a short alias or copied platform value. Agents feed
those names straight back into ``--workspace`` / ``--project`` / ``--group``
flags without ever needing to touch config.toml.
"""

from __future__ import annotations

from typing import Any

import click

from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.formatters.table import display_width, render_table
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError


def _collect_context(cfg: Config) -> dict[str, Any]:
    from inspire.accounts import current_account, list_accounts

    active_account = scrub_raw_ids(current_account() or cfg.username or "") or None

    active_project_name = scrub_raw_ids(cfg.context_project or "") or None
    active_workspace_name = scrub_raw_ids(cfg.context_workspace or "") or None

    # Projects: name + optional path segment (e.g. 'embodied-multimodality').
    projects_by_name: dict[str, dict[str, str]] = {}
    for name in (cfg.projects or {}):
        projects_by_name[scrub_raw_ids(name)] = {"name": scrub_raw_ids(name)}
    for project_id, entry in (cfg.project_catalog or {}).items():
        if not isinstance(entry, dict):
            continue
        catalog_name = entry.get("name")
        path = entry.get("path")
        if not isinstance(catalog_name, str) or not catalog_name.strip():
            # Fall back to reverse lookup from the projects map.
            catalog_name = next(
                (
                    name
                    for name, pid in (cfg.projects or {}).items()
                    if pid == project_id
                ),
                None,
            )
        if not catalog_name:
            continue
        catalog_name = scrub_raw_ids(catalog_name)
        bucket = projects_by_name.setdefault(catalog_name, {"name": catalog_name})
        if isinstance(path, str) and path.strip():
            bucket["path"] = scrub_raw_ids(path.strip())
    projects_view = sorted(projects_by_name.values(), key=lambda e: e["name"])

    # Workspaces: live names from the web session when available.
    ws_name_for_id: dict[str, str] = {}
    try:
        from inspire.config.workspaces import workspace_name_map
        from inspire.platform.web.session import get_web_session

        ws_name_for_id = {
            ws_id: scrub_raw_ids(name)
            for ws_id, name in workspace_name_map(get_web_session()).items()
        }
    except Exception:
        ws_name_for_id = {}
    workspaces_view = sorted(set(ws_name_for_id.values()))

    # Compute groups: name + the workspace name it belongs to (when resolvable).
    compute_groups_view: list[dict[str, Any]] = []
    for group in cfg.compute_groups or []:
        if not isinstance(group, dict):
            continue
        name = str(group.get("name") or "").strip()
        if not name:
            continue
        group_entry: dict[str, Any] = {"name": scrub_raw_ids(name)}
        gpu = str(group.get("gpu_type") or "").strip()
        if gpu:
            group_entry["gpu_type"] = scrub_raw_ids(gpu)
        workspace_ids = group.get("workspace_ids") or []
        workspace_names = [
            ws_name_for_id[ws_id]
            for ws_id in workspace_ids
            if ws_id in ws_name_for_id
        ]
        if workspace_names:
            # compute_groups usually live in a single workspace; flatten to a
            # scalar when that's true.
            group_entry["workspace"] = (
                workspace_names[0] if len(workspace_names) == 1 else workspace_names
            )
        compute_groups_view.append(group_entry)
    compute_groups_view.sort(key=lambda e: (e.get("gpu_type", ""), e["name"]))

    return {
        "active": {
            "account": active_account,
            "project": active_project_name,
            "workspace": active_workspace_name,
        },
        "projects": projects_view,
        "workspaces": workspaces_view,
        "compute_groups": compute_groups_view,
        "accounts": sorted(scrub_raw_ids(account) for account in list_accounts()),
    }


def _render_human(data: dict[str, Any]) -> None:
    def width(header: str, values: list[str], *, max_width: int) -> int:
        return min(max(display_width(header), *(display_width(value) for value in values), 1), max_width)

    active = data["active"]
    click.echo(click.style("Active", bold=True))
    click.echo(f"  account    {active['account'] or '(not set)'}")
    click.echo(f"  project    {active['project'] or '(not set)'}")
    click.echo(f"  workspace  {active['workspace'] or '(not set)'}")
    click.echo()

    projects: list[dict[str, str]] = data["projects"]
    if projects:
        click.echo(click.style(f"Projects ({len(projects)})", bold=True))
        project_rows = [(entry["name"], entry.get("path") or "-") for entry in projects]
        click.echo(
            "\n".join(
                render_table(
                    ("Name", "Path"),
                    project_rows,
                    [
                        width("Name", [row[0] for row in project_rows], max_width=48),
                        width("Path", [row[1] for row in project_rows], max_width=72),
                    ],
                    line_char="─",
                )
            )
        )
        click.echo()

    workspaces: list[str] = data["workspaces"]
    if workspaces:
        click.echo(click.style(f"Workspaces ({len(workspaces)})", bold=True))
        workspace_rows = [(name,) for name in workspaces]
        click.echo(
            "\n".join(
                render_table(
                    ("Name",),
                    workspace_rows,
                    [width("Name", workspaces, max_width=48)],
                    line_char="─",
                )
            )
        )
        click.echo()

    compute_groups: list[dict[str, Any]] = data["compute_groups"]
    if compute_groups:
        click.echo(click.style(f"Compute groups ({len(compute_groups)})", bold=True))
        group_rows: list[tuple[str, str, str]] = []
        for group in compute_groups:
            gpu = group.get("gpu_type")
            workspace = group.get("workspace")
            workspace_text = ""
            if workspace:
                if isinstance(workspace, list):
                    workspace_text = ", ".join(workspace)
                else:
                    workspace_text = str(workspace)
            group_rows.append((str(group["name"]), str(gpu or "-"), workspace_text or "-"))
        click.echo(
            "\n".join(
                render_table(
                    ("Name", "GPU", "Workspace"),
                    group_rows,
                    [
                        width("Name", [row[0] for row in group_rows], max_width=48),
                        width("GPU", [row[1] for row in group_rows], max_width=16),
                        width("Workspace", [row[2] for row in group_rows], max_width=48),
                    ],
                    line_char="─",
                )
            )
        )
        click.echo()

    accounts: list[str] = data["accounts"]
    if accounts:
        click.echo(click.style(f"Accounts ({len(accounts)})", bold=True))
        account_rows = [(name,) for name in accounts]
        click.echo(
            "\n".join(
                render_table(
                    ("Name",),
                    account_rows,
                    [width("Name", accounts, max_width=48)],
                    line_char="─",
                )
            )
        )


@click.command("context")
@pass_context
def show_context(ctx: Context) -> None:
    """Display the active account's projects / workspaces / compute groups.

    All identifiers are platform names (e.g. ``CI-情境智能``, ``H200-3号机房``)
    — never a raw ``ws-…`` / ``project-…`` / ``lcg-…`` ID and never an
    alias. Feed these names straight into ``--workspace`` / ``--project``
    / ``--group`` flags on other commands.

    \b
    Examples:
        inspire config context
        inspire --json config context
    """
    effective_json = ctx.json_output

    try:
        cfg, _sources = Config.from_files_and_env(
            require_credentials=False,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)
        return

    data = _collect_context(cfg)

    if effective_json:
        click.echo(json_formatter.format_json(data))
        return

    _render_human(data)
