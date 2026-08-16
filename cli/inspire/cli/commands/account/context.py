"""Name-only view of the active account context."""

from __future__ import annotations

from typing import Any

import click

from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.collection_output import (
    bound_collection,
    resolve_collection_limit,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError

_CONTEXT_COLLECTION_KEYS = (
    "projects",
    "workspaces",
    "compute_groups",
    "accounts",
)


def _collect_context(cfg: Config) -> dict[str, Any]:
    from inspire.accounts import current_account, list_accounts

    warnings: list[str] = []
    active_account = scrub_raw_ids(current_account() or "") or None

    active_project_name = scrub_raw_ids(cfg.context_project or "") or None
    active_workspace_name = scrub_raw_ids(cfg.context_workspace or "") or None

    # Projects: names only. Local paths are implementation details and are
    # available through path-alias commands when explicitly requested.
    project_names: set[str] = set()
    for name in (cfg.projects or {}):
        project_names.add(scrub_raw_ids(name))
    for entry in (cfg.project_catalog or {}).values():
        if not isinstance(entry, dict):
            continue
        catalog_name = entry.get("name")
        if isinstance(catalog_name, str) and catalog_name.strip():
            project_names.add(scrub_raw_ids(catalog_name))
    projects_view = [{"name": name} for name in sorted(project_names)]

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
        warnings.append(
            "Workspace names are unavailable. Run `inspire account check` and retry."
        )
    workspaces_view = sorted(set(ws_name_for_id.values()))

    # Compute groups: name + workspace name only. GPU and platform metadata are
    # intentionally omitted from this name-discovery command.
    compute_groups_view: list[dict[str, Any]] = []
    for group in cfg.compute_groups or []:
        if not isinstance(group, dict):
            continue
        name = str(group.get("name") or "").strip()
        if not name:
            continue
        group_entry: dict[str, Any] = {"name": scrub_raw_ids(name)}
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
    compute_groups_view.sort(
        key=lambda entry: (
            str(entry.get("workspace") or ""),
            str(entry["name"]),
        )
    )

    data: dict[str, Any] = {
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
    if warnings:
        data["warnings"] = warnings
    return data


def _bound_context(data: dict[str, Any], limit: int | None) -> dict[str, Any]:
    bounded: dict[str, Any] = {"active": data["active"]}
    truncation: dict[str, dict[str, int]] = {}

    for key in _CONTEXT_COLLECTION_KEYS:
        page = bound_collection(data.get(key) or [], limit=limit)
        bounded[key] = page.items
        if page.truncated:
            truncation[key] = {
                "shown": page.shown,
                "total": page.total,
            }

    if truncation:
        bounded["truncated"] = truncation
    warnings = data.get("warnings")
    if isinstance(warnings, list) and warnings:
        bounded["warnings"] = warnings
    return bounded


def _render_human(data: dict[str, Any]) -> None:
    active = data["active"]
    click.echo(
        "active "
        f"account={active['account'] or '-'} "
        f"project={active['project'] or '-'} "
        f"workspace={active['workspace'] or '-'}"
    )

    projects: list[dict[str, str]] = data["projects"]
    for entry in projects:
        click.echo(f"project {entry['name']}")

    workspaces: list[str] = data["workspaces"]
    for name in workspaces:
        click.echo(f"workspace {name}")

    compute_groups: list[dict[str, Any]] = data["compute_groups"]
    for group in compute_groups:
        workspace = group.get("workspace")
        if isinstance(workspace, list):
            workspace_text = ",".join(str(name) for name in workspace)
        else:
            workspace_text = str(workspace or "")
        suffix = f" workspace={workspace_text}" if workspace_text else ""
        click.echo(f"compute-group {group['name']}{suffix}")

    accounts: list[str] = data["accounts"]
    for name in accounts:
        click.echo(f"account {name}")

    truncation = data.get("truncated")
    if isinstance(truncation, dict) and truncation:
        parts = [
            f"{key} {entry['shown']}/{entry['total']}"
            for key, entry in truncation.items()
            if isinstance(entry, dict)
        ]
        if parts:
            click.echo(f"Showing {', '.join(parts)}. Use --all for full lists.")

    warnings = data.get("warnings")
    if isinstance(warnings, list):
        for warning in warnings:
            click.echo(f"Warning: {warning}", err=True)


@click.command("context")
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum names per discovered list (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every discovered name.")
@pass_context
def context(ctx: Context, limit: int | None, show_all: bool) -> None:
    """List names available to the active account.

    Pass the displayed names to ``--workspace``, ``--project``, and
    ``--group`` on other commands.

    \b
    Examples:
        inspire account context
        inspire account context --limit 10
        inspire account context --all
        inspire --json account context
    """
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    try:
        cfg, _ = Config.from_files_and_env(
            require_credentials=False,
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except Exception as e:
        _handle_error(ctx, "Error", str(e), EXIT_GENERAL_ERROR)
        return

    data = _bound_context(_collect_context(cfg), effective_limit)

    if ctx.json_output:
        click.echo(json_formatter.format_json(data))
        return

    _render_human(data)
