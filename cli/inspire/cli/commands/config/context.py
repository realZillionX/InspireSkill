"""``inspire config context`` — name-first view of the active account.

Structured pieces of the loaded config (active account, projects,
workspaces, compute groups) aren't reachable through ``inspire config
show``, which is focused on the flat env-var-backed options. This command
fills that gap with a compact name-only view. The displayed names can be
passed directly to ``--workspace`` / ``--project`` / ``--group``.
"""

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
from inspire.cli.formatters.table import column_width, render_table
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

    active_account = scrub_raw_ids(current_account() or cfg.username or "") or None

    active_project_name = scrub_raw_ids(cfg.context_project or "") or None
    active_workspace_name = scrub_raw_ids(cfg.context_workspace or "") or None

    # Projects: names only. Local paths are implementation details and are
    # available through path-alias commands when explicitly requested.
    project_names: set[str] = set()
    for name in (cfg.projects or {}):
        project_names.add(scrub_raw_ids(name))
    for project_id, entry in (cfg.project_catalog or {}).items():
        if not isinstance(entry, dict):
            continue
        catalog_name = entry.get("name")
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
    return bounded


def _render_human(data: dict[str, Any]) -> None:
    active = data["active"]
    click.echo(
        "Active  "
        f"account    {active['account'] or '-'}  "
        f"project    {active['project'] or '-'}  "
        f"workspace  {active['workspace'] or '-'}"
    )
    click.echo()

    projects: list[dict[str, str]] = data["projects"]
    if projects:
        click.echo(click.style("Projects", bold=True))
        project_rows = [(entry["name"],) for entry in projects]
        click.echo(
            "\n".join(
                render_table(
                    ("Name",),
                    project_rows,
                    [
                        column_width("Name", [row[0] for row in project_rows], max_width=48),
                    ],
                    line_char="─",
                )
            )
        )
        click.echo()

    workspaces: list[str] = data["workspaces"]
    if workspaces:
        click.echo(click.style("Workspaces", bold=True))
        workspace_rows = [(name,) for name in workspaces]
        click.echo(
            "\n".join(
                render_table(
                    ("Name",),
                    workspace_rows,
                    [column_width("Name", workspaces, max_width=48)],
                    line_char="─",
                )
            )
        )
        click.echo()

    compute_groups: list[dict[str, Any]] = data["compute_groups"]
    if compute_groups:
        click.echo(click.style("Compute groups", bold=True))
        group_rows: list[tuple[str, str]] = []
        for group in compute_groups:
            workspace = group.get("workspace")
            workspace_text = ""
            if workspace:
                if isinstance(workspace, list):
                    workspace_text = ", ".join(workspace)
                else:
                    workspace_text = str(workspace)
            group_rows.append((str(group["name"]), workspace_text or "-"))
        click.echo(
            "\n".join(
                render_table(
                    ("Name", "Workspace"),
                    group_rows,
                    [
                        column_width("Name", [row[0] for row in group_rows], max_width=48),
                        column_width(
                            "Workspace",
                            [row[1] for row in group_rows],
                            max_width=48,
                        ),
                    ],
                    line_char="─",
                )
            )
        )
        click.echo()

    accounts: list[str] = data["accounts"]
    if accounts:
        click.echo(click.style("Accounts", bold=True))
        account_rows = [(name,) for name in accounts]
        click.echo(
            "\n".join(
                render_table(
                    ("Name",),
                    account_rows,
                    [column_width("Name", accounts, max_width=48)],
                    line_char="─",
                )
            )
        )

    truncation = data.get("truncated")
    if isinstance(truncation, dict) and truncation:
        parts = [
            f"{key} {entry['shown']}/{entry['total']}"
            for key, entry in truncation.items()
            if isinstance(entry, dict)
        ]
        if parts:
            click.echo()
            click.echo(f"Showing {', '.join(parts)}. Use --all for full lists.")


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
def show_context(ctx: Context, limit: int | None, show_all: bool) -> None:
    """List names available to the active account.

    Pass the displayed names to ``--workspace``, ``--project``, and
    ``--group`` on other commands.

    \b
    Examples:
        inspire config context
        inspire config context --limit 10
        inspire config context --all
        inspire --json config context
    """
    effective_json = ctx.json_output

    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

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

    data = _bound_context(_collect_context(cfg), effective_limit)

    if effective_json:
        click.echo(json_formatter.format_json(data))
        return

    _render_human(data)
