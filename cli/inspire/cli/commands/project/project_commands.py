"""Project subcommands."""

from __future__ import annotations

import concurrent.futures

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.formatters.human_formatter import format_epoch
from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.collection_output import (
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import (
    NAME_PICK_HELP,
    forget_resource_identity,
    reject_id_at_boundary,
    resolve_by_name,
    run_with_stale_handle_retry,
)
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.notebook_cli import (
    WEB_AUTH_HINT,
    require_web_session,
)
from inspire.config import ConfigError
from inspire.config.workspaces import resolve_workspace_query_scope
from inspire.platform.web import browser_api as browser_api_module

_ZERO_WORKSPACE_ID = "ws-00000000-0000-0000-0000-000000000000"
_PROJECT_LIST_MAX_WORKERS = 16
_PROJECT_LIST_WORKSPACE_FANOUT_LIMIT = 6


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _public_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return scrub_raw_ids(value).strip()


def _public_number(value: object) -> int | float | str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = scrub_raw_ids(value).strip()
        return text or None
    return None


def _format_budget(value: object) -> str:
    if value is None:
        return "-"
    try:
        if isinstance(value, (int, float, str)):
            return f"{float(value):,.0f}"
        return str(value)
    except (TypeError, ValueError):
        return str(value)


def _project_to_dict(
    proj: browser_api_module.ProjectInfo,
    *,
    workspace_names_by_id: dict[str, str] | None = None,
) -> dict:
    """Convert a ProjectInfo to the compact, name-only CLI representation."""
    workspace_names = list(proj.workspace_names)
    if not workspace_names and workspace_names_by_id:
        workspace_ids = list(proj.workspace_ids) or [proj.workspace_id]
        for workspace_id in workspace_ids:
            workspace_name = workspace_names_by_id.get(workspace_id)
            if workspace_name and workspace_name not in workspace_names:
                workspace_names.append(workspace_name)

    view: dict[str, object] = {
        "name": scrub_raw_ids(proj.name),
        "workspace": ", ".join(scrub_raw_ids(name) for name in workspace_names),
        "priority": scrub_raw_ids(proj.priority_level or proj.priority_name),
        "remaining_budget": _public_number(proj.member_remain_budget),
    }
    return {
        key: value
        for key, value in view.items()
        if value not in ("", None, [])
    }


def _format_project_list(projects: list[dict]) -> str:
    if not projects:
        return "No projects found."
    rows = [
        (
            str(project.get("name") or ""),
            str(project.get("workspace") or "-"),
            str(project.get("priority") or "-"),
            _format_budget(project.get("remaining_budget")),
        )
        for project in projects
    ]
    widths = [
        column_width("Name", [row[0] for row in rows], max_width=48),
        column_width("Workspace", [row[1] for row in rows], max_width=32),
        column_width("Priority", [row[2] for row in rows], max_width=12),
        column_width("Budget", [row[3] for row in rows], max_width=16),
    ]
    return "\n".join(
        render_table(
            ("Name", "Workspace", "Priority", "Budget"),
            rows,
            widths,
            aligns=["left", "left", "left", "right"],
            line_char="─",
        )
    )


def _project_detail_view(data: dict) -> dict[str, object]:
    owner_value = data.get("creator")
    owner: dict[str, object] = owner_value if isinstance(owner_value, dict) else {}
    view: dict[str, object] = {
        "name": _public_text(data.get("name") or data.get("en_name")),
        "english_name": _public_text(data.get("en_name")),
        "description": _public_text(data.get("description")),
        "budget": _public_number(data.get("budget")),
        "remaining_budget": _public_number(data.get("remain_budget")),
        "priority": _public_text(
            data.get("priority_name") or data.get("priority_level")
        ),
        "created_at": format_epoch(data.get("created_at")) if data.get("created_at") else "",
        "creator": _public_text(owner.get("name")),
    }
    if view["english_name"] == view["name"]:
        view["english_name"] = ""
    return {
        key: value
        for key, value in view.items()
        if value not in ("", None)
    }


def _format_project_detail(project: dict[str, object]) -> str:
    labels = (
        ("Name", "name"),
        ("English name", "english_name"),
        ("Description", "description"),
        ("Budget", "budget"),
        ("Remaining budget", "remaining_budget"),
        ("Priority", "priority"),
        ("Created", "created_at"),
        ("Creator", "creator"),
    )
    return "\n".join(
        f"{label}: {project[key]}"
        for label, key in labels
        if key in project
    )


def _owner_views(items: list[dict]) -> list[dict[str, str]]:
    owners: list[dict[str, str]] = []
    for item in items:
        name = _public_text(item.get("name"))
        if not name:
            continue
        owners.append({"name": name})
    return owners


def _resolve_project_name(
    ctx: Context,
    name: str,
    *,
    session,
    workspace_id: str,
    pick: int | None = None,
    require_live: bool = False,
) -> str:  # noqa: ANN001
    workspace_name = str(
        (getattr(session, "all_workspace_names", None) or {}).get(workspace_id)
        or "the selected workspace"
    ).strip()

    def _lister():
        projects = browser_api_module.list_projects(workspace_id=workspace_id, session=session)
        return [
            {
                "name": project.name,
                "id": project.project_id,
                "status": project.priority_name,
                "created_at": "",
            }
            for project in projects
        ]

    return resolve_by_name(
        ctx,
        name=name,
        resource_type="project",
        list_candidates=_lister,
        pick_index=pick,
        session=session,
        workspace_id=workspace_id,
        require_live=require_live,
        list_command=f"inspire project list --workspace {workspace_name}",
    )


def _unique_workspace_ids(values: list[str | None]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        ws_id = str(value or "").strip()
        if not ws_id or ws_id == _ZERO_WORKSPACE_ID:
            continue
        if ws_id in seen:
            continue
        seen.add(ws_id)
        unique.append(ws_id)
    return unique


def _merge_projects(
    projects: list[browser_api_module.ProjectInfo],
    additional: list[browser_api_module.ProjectInfo],
    *,
    seen: set[str],
) -> None:
    for project in additional:
        if project.project_id not in seen:
            seen.add(project.project_id)
            projects.append(project)
            continue
        for existing in projects:
            if existing.project_id != project.project_id:
                continue
            workspace_ids = _unique_workspace_ids(
                [
                    *existing.workspace_ids,
                    existing.workspace_id,
                    *project.workspace_ids,
                    project.workspace_id,
                ]
            )
            workspace_names = list(existing.workspace_names)
            for workspace_name in project.workspace_names:
                if workspace_name and workspace_name not in workspace_names:
                    workspace_names.append(workspace_name)
            existing.workspace_ids = tuple(workspace_ids)
            existing.workspace_names = tuple(workspace_names)
            break


def _collect_workspace_projects(
    workspace_ids: list[str],
    *,
    session,
) -> tuple[list[browser_api_module.ProjectInfo], list[tuple[str, str]]]:
    """Collect projects across workspace IDs.

    The first workspace is queried serially to establish the request mode
    (HTTP vs browser fallback). Remaining workspaces are fetched in parallel.
    Browser fallback is safe because clients are cached per-thread.
    """
    projects: list[browser_api_module.ProjectInfo] = []
    seen: set[str] = set()
    workspace_errors: list[tuple[str, str]] = []

    if not workspace_ids:
        return projects, workspace_errors

    first_ws_id = workspace_ids[0]
    try:
        first_projects = browser_api_module.list_projects(workspace_id=first_ws_id, session=session)
        _merge_projects(projects, first_projects, seen=seen)
    except Exception as exc:
        workspace_errors.append((first_ws_id, str(exc)))

    remaining_ws_ids = workspace_ids[1:]
    if not remaining_ws_ids:
        return projects, workspace_errors

    if len(remaining_ws_ids) > 1:
        max_workers = min(len(remaining_ws_ids), _PROJECT_LIST_MAX_WORKERS)
        results_by_workspace: dict[str, list[browser_api_module.ProjectInfo]] = {}
        errors_by_workspace: dict[str, str] = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    browser_api_module.list_projects, workspace_id=ws_id, session=session
                ): ws_id
                for ws_id in remaining_ws_ids
            }
            for future in concurrent.futures.as_completed(futures):
                ws_id = futures[future]
                try:
                    results_by_workspace[ws_id] = future.result()
                except Exception as exc:
                    errors_by_workspace[ws_id] = str(exc)

        for ws_id in remaining_ws_ids:
            if ws_id in errors_by_workspace:
                workspace_errors.append((ws_id, errors_by_workspace[ws_id]))
                continue
            _merge_projects(projects, results_by_workspace.get(ws_id, []), seen=seen)
        return projects, workspace_errors

    for ws_id in remaining_ws_ids:
        try:
            ws_projects = browser_api_module.list_projects(workspace_id=ws_id, session=session)
            _merge_projects(projects, ws_projects, seen=seen)
        except Exception as exc:
            workspace_errors.append((ws_id, str(exc)))
    return projects, workspace_errors


def _collect_all_workspace_projects(
    workspace_ids: list[str],
    *,
    session,
) -> tuple[list[browser_api_module.ProjectInfo], list[tuple[str, str]]]:
    """Collect all visible projects, preferring the single project-scoped endpoint."""
    try:
        projects = browser_api_module.list_all_projects(session=session)
        if any(p.workspace_id or p.workspace_ids or p.workspace_names for p in projects):
            return projects, []
        workspace_projects, workspace_errors = _collect_workspace_projects(
            workspace_ids,
            session=session,
        )
        if workspace_projects:
            return workspace_projects, workspace_errors
        return projects, []
    except Exception:
        return _collect_workspace_projects(workspace_ids, session=session)


def _select_workspace_ids_for_listing(
    workspace_ids: list[str],
    *,
    session_workspace_id: str | None,
    all_workspaces: bool,
) -> list[str]:
    if all_workspaces or len(workspace_ids) <= _PROJECT_LIST_WORKSPACE_FANOUT_LIMIT:
        return workspace_ids

    selected: list[str] = []
    seen: set[str] = set()

    preferred = str(session_workspace_id or "").strip()
    if preferred and preferred in workspace_ids:
        selected.append(preferred)
        seen.add(preferred)

    for ws_id in workspace_ids:
        if ws_id in seen:
            continue
        selected.append(ws_id)
        seen.add(ws_id)
        if len(selected) >= _PROJECT_LIST_WORKSPACE_FANOUT_LIMIT:
            break

    return selected


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@click.command("list")
@click.option(
    "--workspace",
    required=True,
    metavar="NAME|all",
    help="Workspace name or 'all'.",
)
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum projects to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every matching project.")
@pass_context
def list_projects_cmd(
    ctx: Context,
    workspace: str,
    limit: int | None,
    show_all: bool,
) -> None:
    """List project-level metadata.

    \b
    Examples:
        inspire project list --workspace all
        inspire --json project list --workspace all
    """
    json_output = ctx.json_output
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )
    try:
        workspace_ids, all_workspaces = resolve_workspace_query_scope(
            workspace=workspace,
            session=session,
        )
        if all_workspaces:
            projects, workspace_errors = _collect_all_workspace_projects(
                workspace_ids,
                session=session,
            )
        else:
            query_workspace_ids = _select_workspace_ids_for_listing(
                workspace_ids,
                session_workspace_id=None,
                all_workspaces=all_workspaces,
            )
            projects, workspace_errors = _collect_workspace_projects(
                query_workspace_ids,
                session=session,
            )
        if not projects and workspace_errors:
            workspace_names = dict(
                getattr(session, "all_workspace_names", None) or {}
            )
            error_samples = ", ".join(
                f"{scrub_raw_ids(workspace_names.get(ws_id) or '(workspace)')}: "
                f"{scrub_raw_ids(message)}"
                for ws_id, message in workspace_errors[:3]
            )
            if len(workspace_errors) > 3:
                error_samples += ", ..."
            raise ValueError(
                f"Failed to list projects across requested workspaces "
                f"({len(workspace_errors)} failed: {error_samples})"
            )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", scrub_raw_ids(e), EXIT_CONFIG_ERROR)
        return
    except Exception as e:
        _handle_error(
            ctx,
            "APIError",
            f"Failed to list projects: {scrub_raw_ids(e)}",
            EXIT_API_ERROR,
        )
        return

    session_workspace_names = dict(getattr(session, "all_workspace_names", None) or {})
    results = [_project_to_dict(p, workspace_names_by_id=session_workspace_names) for p in projects]
    page = bound_collection(results, limit=effective_limit)

    if json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "items": page.items,
                    **page.metadata(),
                }
            )
        )
        return

    click.echo(_format_project_list(page.items))
    notice = truncation_notice(page)
    if notice:
        click.echo(notice)


@click.command("detail")
@click.argument("project", metavar="NAME")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--pick",
    type=click.IntRange(1),
    default=None,
    help=NAME_PICK_HELP,
)
@pass_context
def detail_project_cmd(
    ctx: Context,
    project: str,
    workspace: str,
    pick: int | None,
) -> None:
    """Show detail for a single project by name."""
    project = reject_id_at_boundary(
        ctx,
        project,
        resource_type="project",
        list_command="inspire project list --workspace <workspace>",
    )
    session = require_web_session(ctx, hint="inspire project detail requires a logged-in web session")
    try:
        workspace_ids, is_all = resolve_workspace_query_scope(
            workspace=workspace,
            session=session,
        )
        if is_all:
            raise ConfigError("project detail requires a single workspace name, not 'all'.")
        project_id, data = run_with_stale_handle_retry(
            name=project,
            resolve_cached=lambda: _resolve_project_name(
                ctx,
                project,
                session=session,
                workspace_id=workspace_ids[0],
                pick=pick,
            ),
            resolve_live=lambda live_name: _resolve_project_name(
                ctx,
                live_name,
                session=session,
                workspace_id=workspace_ids[0],
                pick=pick,
                require_live=True,
            ),
            operation=lambda resolved_project_id: (
                resolved_project_id,
                browser_api_module.get_project_detail(
                    resolved_project_id,
                    session=session,
                ),
            ),
            invalidate=lambda resolved_project_id: forget_resource_identity(
                session=session,
                resource_type="project",
                resource_id=resolved_project_id,
                name=project,
                workspace_id=workspace_ids[0],
            ),
        )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", scrub_raw_ids(e), EXIT_CONFIG_ERROR)
        return
    except Exception as e:
        _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)
        return

    view = _project_detail_view(data)
    if ctx.json_output:
        click.echo(json_formatter.format_json(view))
        return

    click.echo(_format_project_detail(view))


@click.command("owners")
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum owners to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every project owner.")
@pass_context
def owners_project_cmd(
    ctx: Context,
    limit: int | None,
    show_all: bool,
) -> None:
    """List candidate project owners."""
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    session = require_web_session(ctx, hint="inspire project owners requires a logged-in web session")
    try:
        items = browser_api_module.list_project_owners(session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)
        return

    owners = _owner_views(items)
    page = bound_collection(owners, limit=effective_limit)
    if ctx.json_output:
        payload: dict[str, object] = {"items": page.items, **page.metadata()}
        click.echo(json_formatter.format_json(payload))
        return

    if not page.items:
        click.echo("No project owners returned.")
        return

    rows = [(owner["name"],) for owner in page.items]
    widths = [column_width("Name", [row[0] for row in rows], max_width=48)]
    click.echo("\n".join(render_table(("Name",), rows, widths, line_char="─")))
    notice = truncation_notice(page)
    if notice:
        click.echo(notice)
