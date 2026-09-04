"""Show the platform permissions granted to the active account."""

from __future__ import annotations

from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.cli.formatters import json_formatter
from inspire.cli.formatters.table import column_width, render_table
from inspire.cli.utils.collection_output import (
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.config.workspaces import (
    resolve_workspace_query_scope,
    workspace_name_map,
)
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session


@click.command("permissions")
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
    help="Maximum permission names to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every permission name.")
@pass_context
def permissions(
    ctx: Context,
    workspace: Optional[str],
    limit: int | None,
    show_all: bool,
) -> None:
    """Show granted permissions by workspace."""
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_ids, all_workspaces = resolve_workspace_query_scope(
            workspace=workspace,
            session=session,
        )
        permissions: list[str | dict[str, str]]
        if all_workspaces:
            workspace_names = workspace_name_map(session)
            permissions = []
            for workspace_id in workspace_ids:
                workspace_name = scrub_raw_ids(
                    workspace_names.get(workspace_id) or "(workspace name unavailable)"
                )
                permissions.extend(
                    {
                        "workspace": workspace_name,
                        "permission": scrub_raw_ids(permission),
                    }
                    for permission in sorted(
                        set(
                            browser_api_module.get_user_permissions(
                                workspace_id=workspace_id,
                                session=session,
                            )
                        )
                    )
                )
        else:
            permissions = [
                scrub_raw_ids(permission)
                for permission in sorted(
                    set(
                        browser_api_module.get_user_permissions(
                            workspace_id=workspace_ids[0],
                            session=session,
                        )
                    )
                )
            ]
        page = bound_collection(permissions, limit=effective_limit)

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {
                        "items": page.items,
                        **page.metadata(),
                    }
                )
            )
            return

        if not page.items:
            click.echo(
                "No permissions granted in the requested workspaces."
                if all_workspaces
                else "No permissions granted in this workspace."
            )
            return

        fallback_workspace = scrub_raw_ids(
            workspace_name_map(session).get(workspace_ids[0])
            or "(workspace name unavailable)"
        )
        table_rows = [
            (
                scrub_raw_ids(
                    f"{permission.get('workspace') or fallback_workspace}: "
                    f"{permission.get('permission') or ''}"
                )
                if isinstance(permission, dict)
                else scrub_raw_ids(f"{fallback_workspace}: {permission}"),
            )
            for permission in page.items
        ]
        widths = [
            column_width("Permission", [row[0] for row in table_rows], max_width=112),
        ]
        click.echo(
            "\n".join(
                render_table(
                    ("Permission",),
                    table_rows,
                    widths,
                )
            )
        )
        notice = truncation_notice(page)
        if notice:
            click.echo(notice)

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
