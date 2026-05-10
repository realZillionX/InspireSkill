"""Project subcommands."""

from __future__ import annotations

import concurrent.futures

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.formatters.human_formatter import format_epoch
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.id_resolver import resolve_by_name
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.notebook_cli import (
    WEB_AUTH_HINT,
    require_web_session,
    resolve_json_output,
)
from inspire.platform.web import browser_api as browser_api_module

_ZERO_WORKSPACE_ID = "ws-00000000-0000-0000-0000-000000000000"
_PROJECT_LIST_MAX_WORKERS = 16
_PROJECT_LIST_WORKSPACE_FANOUT_LIMIT = 6


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_to_dict(proj: browser_api_module.ProjectInfo) -> dict:
    """Convert a ProjectInfo to a plain dict for JSON output."""
    return {
        "project_id": proj.project_id,
        "name": proj.name,
        "workspace_id": proj.workspace_id,
        "budget": proj.budget,
        "remain_budget": proj.remain_budget,
        "member_remain_budget": proj.member_remain_budget,
        "gpu_limit": proj.gpu_limit,
        "member_gpu_limit": proj.member_gpu_limit,
        "priority_level": proj.priority_level,
        "priority_name": proj.priority_name,
    }


def _resolve_project_name(ctx: Context, name: str, *, session) -> str:  # noqa: ANN001
    def _lister():
        projects = browser_api_module.list_projects(session=session)
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
    "--json",
    "json_output",
    is_flag=True,
    help="Alias for global --json",
)
@click.option(
    "--all-workspaces",
    "all_workspaces",
    is_flag=True,
    default=True,
    help="Query all discovered workspaces (default, exhaustive).",
)
@pass_context
def list_projects_cmd(
    ctx: Context,
    json_output: bool,
    all_workspaces: bool,
) -> None:
    """List projects and their GPU quota.

    \b
    Examples:
        inspire project list          # Show project quota table
        inspire project list --json   # JSON output with all fields
    """
    json_output = resolve_json_output(ctx, json_output)

    session = require_web_session(
        ctx,
        hint=WEB_AUTH_HINT,
    )

    try:
        workspace_ids = session.all_workspace_ids
        if workspace_ids is None:
            # Workspace discovery never happened (stale session or login
            # method that doesn't support it). Fall back to the session's
            # single workspace.
            workspace_ids = _unique_workspace_ids(
                [getattr(session, "workspace_id", None)]
            )
        else:
            workspace_ids = _unique_workspace_ids(list(workspace_ids))
        if not workspace_ids:
            # No discovered workspaces — only default query path applies.
            projects = browser_api_module.list_projects(session=session)
        elif not all_workspaces:
            # API-side reduction: prefer a single default project-list query
            # before probing per-workspace platform data.
            default_query_error: Exception | None = None
            try:
                projects = browser_api_module.list_projects(session=session)
            except Exception as exc:
                projects = []
                default_query_error = exc

            if not projects:
                query_workspace_ids = _select_workspace_ids_for_listing(
                    workspace_ids,
                    session_workspace_id=getattr(session, "workspace_id", None),
                    all_workspaces=False,
                )
                projects, workspace_errors = _collect_workspace_projects(
                    query_workspace_ids,
                    session=session,
                )
                if not projects and workspace_errors and default_query_error is not None:
                    error_samples = ", ".join(
                        f"{ws_id}: {message}" for ws_id, message in workspace_errors[:3]
                    )
                    if len(workspace_errors) > 3:
                        error_samples += ", ..."
                    raise ValueError(
                        f"Failed to list projects across visible workspaces "
                        f"({len(workspace_errors)} failed: {error_samples}); "
                        f"default query failed: {default_query_error}"
                    ) from default_query_error
        else:
            query_workspace_ids = _select_workspace_ids_for_listing(
                workspace_ids,
                session_workspace_id=getattr(session, "workspace_id", None),
                all_workspaces=all_workspaces,
            )
            projects, workspace_errors = _collect_workspace_projects(
                query_workspace_ids,
                session=session,
            )
            if not projects and workspace_errors:
                try:
                    projects = browser_api_module.list_projects(session=session)
                except Exception as e:
                    error_samples = ", ".join(
                        f"{ws_id}: {message}" for ws_id, message in workspace_errors[:3]
                    )
                    if len(workspace_errors) > 3:
                        error_samples += ", ..."
                    raise ValueError(
                        f"Failed to list projects across visible workspaces "
                        f"({len(workspace_errors)} failed: {error_samples}); "
                        f"default query failed: {e}"
                    ) from e
    except Exception as e:
        _handle_error(ctx, "APIError", f"Failed to list projects: {e}", EXIT_API_ERROR)
        return

    results = [_project_to_dict(p) for p in projects]

    if json_output:
        click.echo(json_formatter.format_json({"projects": results, "total": len(results)}))
        return

    click.echo(human_formatter.format_project_list(results))


@click.command("detail")
@click.argument("project")
@pass_context
def detail_project_cmd(ctx: Context, project: str) -> None:
    """Show detail for a single project by name."""
    session = require_web_session(ctx, hint="inspire project detail requires a logged-in web session")
    try:
        project_id = _resolve_project_name(ctx, project, session=session)
        data = browser_api_module.get_project_detail(project_id, session=session)
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
