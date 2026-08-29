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
)


def _collect_context(cfg: Config) -> dict[str, Any]:
    from inspire.accounts import current_account
    from inspire.config.workspaces import workspace_name_map
    from inspire.platform.web import browser_api as browser_api_module
    from inspire.platform.web.session import get_web_session

    warnings: list[str] = []
    active_account = scrub_raw_ids(current_account() or "") or None

    # One live session feeds every catalog below. Account config deliberately
    # carries no project or compute-group snapshot, so falling back to Config
    # here would quietly reintroduce the stale-catalog bug that init removes.
    session: Any | None = None
    ws_name_for_id: dict[str, str] = {}
    try:
        session = get_web_session()
        for workspace_id, raw_name in workspace_name_map(session).items():
            name = scrub_raw_ids(raw_name)
            if name:
                ws_name_for_id[workspace_id] = name
    except Exception:
        warnings.append(
            "Workspace names are unavailable. Run `inspire account check` and retry."
        )
    workspaces_view = sorted(set(ws_name_for_id.values()))

    # Projects are global objects that can span workspaces, so use the one-call
    # live project listing instead of querying once per workspace.
    projects_view: list[dict[str, str]] = []
    if session is not None:
        try:
            project_names = {
                scrub_raw_ids(str(getattr(project, "name", "") or "").strip())
                for project in browser_api_module.list_all_projects(session=session)
            }
            projects_view = [
                {"name": name}
                for name in sorted(project_names)
                if name
            ]
        except Exception:
            warnings.append(
                "Project names are unavailable. Run `inspire account check` and retry."
            )

    # Compute groups are workspace-scoped. Preserve successful workspace rows
    # when one workspace fails, but say that the aggregate is incomplete.
    compute_groups_view: list[dict[str, Any]] = []
    if session is not None:
        group_workspaces: dict[str, set[str]] = {}
        failed_workspace_count = 0
        for workspace_id, workspace_name in sorted(
            ws_name_for_id.items(),
            key=lambda item: (item[1], item[0]),
        ):
            try:
                groups = browser_api_module.list_compute_groups(
                    workspace_id=workspace_id,
                    session=session,
                )
            except Exception:
                failed_workspace_count += 1
                continue
            for group in groups:
                if not isinstance(group, dict):
                    continue
                raw_name = (
                    group.get("name")
                    or group.get("logic_compute_group_name")
                    or group.get("compute_group_name")
                    or ""
                )
                name = scrub_raw_ids(str(raw_name).strip())
                if name:
                    group_workspaces.setdefault(name, set()).add(workspace_name)

        for name, workspace_names_set in group_workspaces.items():
            workspace_names = sorted(workspace_names_set)
            entry: dict[str, Any] = {"name": name}
            if workspace_names:
                entry["workspace"] = (
                    workspace_names[0]
                    if len(workspace_names) == 1
                    else workspace_names
                )
            compute_groups_view.append(entry)
        compute_groups_view.sort(
            key=lambda entry: (
                str(entry.get("workspace") or ""),
                str(entry["name"]),
            )
        )
        if failed_workspace_count:
            warnings.append(
                "Compute group names are incomplete: "
                f"{failed_workspace_count} workspace(s) could not be queried. "
                "Run `inspire account check` and retry."
            )

    data: dict[str, Any] = {
        "active": {
            "account": active_account,
        },
        "projects": projects_view,
        "workspaces": workspaces_view,
        "compute_groups": compute_groups_view,
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
        "active " f"account={active['account'] or '-'}"
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
    """List live names available to the active account.

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
