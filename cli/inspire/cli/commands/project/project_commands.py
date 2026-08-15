"""Project subcommands.

A project is a global object, not a workspace-scoped one: `ListProjects`
answers the same set with or without a workspace filter, `GetProjectDetail`
addresses the project alone, and the visible-workspace list the rows carry is
an attribute rather than a scope. Nothing here takes `--workspace`.
"""

from __future__ import annotations

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
from inspire.platform.web import browser_api as browser_api_module


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


def _project_to_dict(proj: browser_api_module.ProjectInfo) -> dict:
    """Convert a ProjectInfo to the compact, name-only CLI representation."""
    view: dict[str, object] = {
        "name": scrub_raw_ids(proj.name),
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
            str(project.get("priority") or "-"),
            _format_budget(project.get("remaining_budget")),
        )
        for project in projects
    ]
    widths = [
        column_width("Name", [row[0] for row in rows], max_width=48),
        column_width("Priority", [row[1] for row in rows], max_width=12),
        column_width("Budget", [row[2] for row in rows], max_width=16),
    ]
    return "\n".join(
        render_table(
            ("Name", "Priority", "Budget"),
            rows,
            widths,
            aligns=["left", "left", "right"],
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
    pick: int | None = None,
    require_live: bool = False,
) -> str:  # noqa: ANN001
    """Resolve a project name against the whole visible catalog.

    Projects are global, so the candidate set is too. Narrowing by workspace
    would only hide a project that is visible elsewhere behind a "not found",
    and `GetProjectDetail` takes the project id alone anyway.
    """
    def _lister():
        projects = browser_api_module.list_all_projects(session=session)
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
        require_live=require_live,
        list_command="inspire project list",
    )


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@click.command("list")
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
    limit: int | None,
    show_all: bool,
) -> None:
    """List every project visible to this account.

    \b
    Projects are not scoped to a workspace, so this takes no `--workspace`:
    `ListProjects` answers the same set with or without a workspace filter,
    and one unfiltered call replaces a fanout over every visible workspace.

    \b
    Examples:
        inspire project list
        inspire --json project list
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
        projects = browser_api_module.list_all_projects(session=session)
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

    results = [_project_to_dict(p) for p in projects]
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
    pick: int | None,
) -> None:
    """Show detail for a single project by name.

    Projects are not scoped to a workspace, so this takes no `--workspace`:
    `GetProjectDetail` addresses the project alone.
    """
    project = reject_id_at_boundary(
        ctx,
        project,
        resource_type="project",
        list_command="inspire project list",
    )
    session = require_web_session(ctx, hint="inspire project detail requires a logged-in web session")
    try:
        project_id, data = run_with_stale_handle_retry(
            name=project,
            resolve_cached=lambda: _resolve_project_name(
                ctx,
                project,
                session=session,
                pick=pick,
            ),
            resolve_live=lambda live_name: _resolve_project_name(
                ctx,
                live_name,
                session=session,
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
