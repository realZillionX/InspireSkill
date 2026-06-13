"""Project subcommands."""

from __future__ import annotations

import concurrent.futures

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_CONFIG_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.formatters.human_formatter import format_epoch
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import resolve_by_name
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.notebook_cli import (
    WEB_AUTH_HINT,
    load_config,
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


def _project_to_dict(
    proj: browser_api_module.ProjectInfo,
    *,
    workspace_names_by_id: dict[str, str] | None = None,
) -> dict:
    """Convert a ProjectInfo to a plain dict for JSON output."""
    workspace_names = list(proj.workspace_names)
    if not workspace_names and workspace_names_by_id:
        workspace_ids = list(proj.workspace_ids) or [proj.workspace_id]
        for workspace_id in workspace_ids:
            workspace_name = workspace_names_by_id.get(workspace_id)
            if workspace_name and workspace_name not in workspace_names:
                workspace_names.append(workspace_name)

    return {
        "project_id": proj.project_id,
        "name": proj.name,
        "workspace_id": proj.workspace_id,
        "workspace_ids": list(proj.workspace_ids),
        "workspace_names": workspace_names,
        "budget": proj.budget,
        "remain_budget": proj.remain_budget,
        "member_remain_budget": proj.member_remain_budget,
        "gpu_limit": proj.gpu_limit,
        "member_gpu_limit": proj.member_gpu_limit,
        "priority_level": proj.priority_level,
        "priority_name": proj.priority_name,
    }


def _resolve_project_name(ctx: Context, name: str, *, session, workspace_id: str) -> str:  # noqa: ANN001
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
        json_output=ctx.json_output,
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
    help="Workspace name or 'all'.",
)
@pass_context
def list_projects_cmd(
    ctx: Context,
    workspace: str,
) -> None:
    """List project-level metadata.

    \b
    Examples:
        inspire project list --workspace all
        inspire --json project list --workspace all
    """
    json_output = ctx.json_output

    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )
    config = load_config(ctx)

    try:
        workspace_ids, all_workspaces = resolve_workspace_query_scope(
            config,
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
            error_samples = ", ".join(
                f"{ws_id}: {message}" for ws_id, message in workspace_errors[:3]
            )
            if len(workspace_errors) > 3:
                error_samples += ", ..."
            raise ValueError(
                f"Failed to list projects across requested workspaces "
                f"({len(workspace_errors)} failed: {error_samples})"
            )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to list projects: {e}", EXIT_API_ERROR)
        return

    session_workspace_names = dict(getattr(session, "all_workspace_names", None) or {})
    results = [_project_to_dict(p, workspace_names_by_id=session_workspace_names) for p in projects]

    if json_output:
        click.echo(json_formatter.format_json({"projects": results, "total": len(results)}))
        return

    click.echo(human_formatter.format_project_list(results))


@click.command("detail")
@click.argument("project")
@click.option("--workspace", required=True, help="Workspace name.")
@pass_context
def detail_project_cmd(ctx: Context, project: str, workspace: str) -> None:
    """Show detail for a single project by name."""
    session = require_web_session(ctx, hint="inspire project detail requires a logged-in web session")
    config = load_config(ctx)
    try:
        workspace_ids, is_all = resolve_workspace_query_scope(
            config,
            workspace=workspace,
            session=session,
        )
        if is_all:
            raise ConfigError("project detail requires a single workspace name, not 'all'.")
        project_id = _resolve_project_name(ctx, project, session=session, workspace_id=workspace_ids[0])
        data = browser_api_module.get_project_detail(project_id, session=session)
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    if ctx.json_output:
        click.echo(json_formatter.format_json(data))
        return

    click.echo("Project")
    click.echo(f"  Name:          {scrub_raw_ids(data.get('name') or data.get('en_name') or 'N/A')}")
    if data.get("en_name") and data.get("en_name") != data.get("name"):
        click.echo(f"  English name:  {scrub_raw_ids(data.get('en_name'))}")
    if data.get("description"):
        click.echo(f"  Description:   {scrub_raw_ids(data.get('description'))}")
    if data.get("budget"):
        click.echo(f"  Budget:        {data.get('budget')}")
    if data.get("children_budget"):
        click.echo(f"  Children bgt:  {data.get('children_budget')}")
    if data.get("priority_name"):
        click.echo(
            f"  Priority:      {scrub_raw_ids(data.get('priority_name'))} "
            f"({scrub_raw_ids(data.get('priority_level', '?'))})"
        )
    if data.get("created_at"):
        click.echo(f"  Created:       {format_epoch(data.get('created_at'))}")
    owner = data.get("creator") if isinstance(data.get("creator"), dict) else None
    if owner:
        click.echo(f"  Creator:       {scrub_raw_ids(owner.get('name') or '?')}")


@click.command("owners")
@pass_context
def owners_project_cmd(ctx: Context) -> None:
    """List candidate project owners."""
    session = require_web_session(ctx, hint="inspire project owners requires a logged-in web session")
    try:
        items = browser_api_module.list_project_owners(session=session)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
        return

    if ctx.json_output:
        click.echo(json_formatter.format_json({"total": len(items), "items": items}))
        return

    if not items:
        click.echo("No project owners returned.")
        return

    click.echo(f"Project Owners ({len(items)})")
    for i, it in enumerate(items, 1):
        name = it.get("name") or it.get("id") or "?"
        login = (it.get("extra_info") or {}).get("login_name", "")
        extra = f" ({login})" if login else ""
        click.echo(f"  [{i}] {name}{extra}")
