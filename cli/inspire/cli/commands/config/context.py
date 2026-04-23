"""``inspire config context`` — agent-facing view of the active account.

Structured pieces of the loaded config (active account, projects,
workspaces, compute groups) aren't reachable through ``inspire config
show``, which is focused on the flat env-var-backed options. This command
fills that gap with a **name-only** view: every workspace, project, and
compute group is identified by its platform name (``CI-情境智能``,
``H200-3号机房``), not by a short alias or a raw ``ws-…`` ID. Agents feed
those names straight back into ``--workspace`` / ``--project`` /
``--compute-group`` flags without ever needing to touch config.toml.
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
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.config import Config, ConfigError


def _project_name_for_id(cfg: Config, project_id: str | None) -> str | None:
    if not project_id:
        return None
    for name, pid in (cfg.projects or {}).items():
        if pid == project_id:
            return name
    catalog_entry = (cfg.project_catalog or {}).get(project_id)
    if isinstance(catalog_entry, dict):
        catalog_name = catalog_entry.get("name")
        if isinstance(catalog_name, str) and catalog_name.strip():
            return catalog_name.strip()
    return None


def _workspace_name_for_id(cfg: Config, workspace_id: str | None) -> str | None:
    if not workspace_id:
        return None
    for name, ws in (cfg.workspaces or {}).items():
        if ws == workspace_id:
            return name
    return None


def _collect_context(cfg: Config) -> dict[str, Any]:
    from inspire.accounts import current_account, list_accounts

    active_account = current_account() or cfg.username or None

    active_project_name = _project_name_for_id(cfg, cfg.job_project_id)
    active_workspace_name = _workspace_name_for_id(cfg, cfg.job_workspace_id)

    # Projects: name + optional path segment (e.g. 'embodied-multimodality').
    projects_by_name: dict[str, dict[str, str]] = {}
    for name in (cfg.projects or {}):
        projects_by_name[name] = {"name": name}
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
        bucket = projects_by_name.setdefault(catalog_name, {"name": catalog_name})
        if isinstance(path, str) and path.strip():
            bucket["path"] = path.strip()
    projects_view = sorted(projects_by_name.values(), key=lambda e: e["name"])

    # Workspaces: just the name list, sorted.
    workspaces_view = sorted(
        name
        for name in (cfg.workspaces or {})
        if isinstance(name, str) and name.strip()
    )

    # Compute groups: name + the workspace name it belongs to (when resolvable).
    ws_name_for_id: dict[str, str] = {
        ws_id: name for name, ws_id in (cfg.workspaces or {}).items()
    }
    compute_groups_view: list[dict[str, Any]] = []
    for group in cfg.compute_groups or []:
        if not isinstance(group, dict):
            continue
        name = str(group.get("name") or "").strip()
        if not name:
            continue
        entry: dict[str, Any] = {"name": name}
        gpu = str(group.get("gpu_type") or "").strip()
        if gpu:
            entry["gpu_type"] = gpu
        workspace_ids = group.get("workspace_ids") or []
        workspace_names = [
            ws_name_for_id[ws_id]
            for ws_id in workspace_ids
            if ws_id in ws_name_for_id
        ]
        if workspace_names:
            # compute_groups usually live in a single workspace; flatten to a
            # scalar when that's true.
            entry["workspace"] = (
                workspace_names[0] if len(workspace_names) == 1 else workspace_names
            )
        compute_groups_view.append(entry)
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
        "accounts": sorted(list_accounts()),
    }


def _render_human(data: dict[str, Any]) -> None:
    active = data["active"]
    click.echo(click.style("Active", bold=True))
    click.echo(f"  account    {active['account'] or '(not set)'}")
    click.echo(f"  project    {active['project'] or '(not set)'}")
    click.echo(f"  workspace  {active['workspace'] or '(not set)'}")
    click.echo()

    projects: list[dict[str, str]] = data["projects"]
    if projects:
        click.echo(click.style(f"Projects ({len(projects)})", bold=True))
        name_width = max(len(p["name"]) for p in projects)
        for entry in projects:
            path = entry.get("path")
            suffix = f"  (path: {path})" if path else ""
            click.echo(f"  {entry['name'].ljust(name_width)}{suffix}")
        click.echo()

    workspaces: list[str] = data["workspaces"]
    if workspaces:
        click.echo(click.style(f"Workspaces ({len(workspaces)})", bold=True))
        for name in workspaces:
            click.echo(f"  {name}")
        click.echo()

    compute_groups: list[dict[str, Any]] = data["compute_groups"]
    if compute_groups:
        click.echo(click.style(f"Compute groups ({len(compute_groups)})", bold=True))
        name_width = max(len(g["name"]) for g in compute_groups)
        for group in compute_groups:
            bits: list[str] = []
            gpu = group.get("gpu_type")
            if gpu:
                bits.append(f"gpu={gpu}")
            workspace = group.get("workspace")
            if workspace:
                if isinstance(workspace, list):
                    bits.append(f"workspaces={'+'.join(workspace)}")
                else:
                    bits.append(f"workspace={workspace}")
            suffix = f"  ({', '.join(bits)})" if bits else ""
            click.echo(f"  {group['name'].ljust(name_width)}{suffix}")
        click.echo()

    accounts: list[str] = data["accounts"]
    if accounts:
        click.echo(click.style(f"Accounts ({len(accounts)})", bold=True))
        for name in accounts:
            click.echo(f"  {name}")


@click.command("context")
@click.option(
    "--json",
    "json_output_local",
    is_flag=True,
    help="Output as JSON (machine-readable). Equivalent to the top-level --json.",
)
@pass_context
def show_context(ctx: Context, json_output_local: bool) -> None:
    """Display the active account's projects / workspaces / compute groups.

    All identifiers are platform names (e.g. ``CI-情境智能``, ``H200-3号机房``)
    — never a raw ``ws-…`` / ``project-…`` / ``lcg-…`` ID and never an
    alias. Feed these names straight into ``--workspace`` / ``--project``
    / ``--compute-group`` flags on other commands.

    \b
    Examples:
        inspire config context
        inspire config context --json
    """
    effective_json = bool(ctx.json_output or json_output_local)

    try:
        cfg, _sources = Config.from_files_and_env(
            require_credentials=False,
            require_target_dir=False,
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
