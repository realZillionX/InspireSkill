"""Ray (弹性计算) job commands for Inspire CLI."""

from __future__ import annotations

from typing import Optional

import click

from inspire.cli.context import (
    Context,
    EXIT_API_ERROR,
    EXIT_AUTH_ERROR,
    EXIT_CONFIG_ERROR,
    pass_context,
)
from inspire.cli.formatters import human_formatter, json_formatter
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _format_ray_list_rows(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "No Ray jobs found."

    id_w = max(len("Ray Job ID"), *(len(r["ray_job_id"]) for r in rows))
    name_w = max(len("Name"), *(len(r["name"]) for r in rows))
    status_w = max(len("Status"), *(len(r["status"]) for r in rows))
    created_w = max(len("Created"), *(len(r["created_at"]) for r in rows))
    user_w = max(len("Created By"), *(len(r["created_by_name"]) for r in rows))

    header = (
        f"{'Ray Job ID':<{id_w}} {'Name':<{name_w}} "
        f"{'Status':<{status_w}} {'Created':<{created_w}} "
        f"{'Created By':<{user_w}}"
    )
    sep = "-" * len(header)
    lines = ["Ray Jobs (弹性计算)", header, sep]
    for row in rows:
        lines.append(
            f"{row['ray_job_id']:<{id_w}} "
            f"{row['name']:<{name_w}} "
            f"{row['status']:<{status_w}} "
            f"{row['created_at']:<{created_w}} "
            f"{row['created_by_name']:<{user_w}}"
        )
    lines.append(sep)
    lines.append(f"Total: {len(rows)}")
    return "\n".join(lines)


@click.command("list")
@click.option("--workspace", default=None, help="Workspace name (from [workspaces])")
@click.option(
    "--workspace-id",
    "workspace_id_override",
    default=None,
    help="Workspace ID override (ws-…)",
)
@click.option(
    "--all-users",
    "-A",
    is_flag=True,
    help="Include jobs from all users (default: only the current user).",
)
@click.option(
    "--created-by",
    "created_by",
    default=None,
    help="Filter by creator user ID (user-…); repeatable-friendly comma-separated list.",
)
@click.option("--page-num", type=int, default=1, show_default=True, help="Page number")
@click.option("--page-size", type=int, default=20, show_default=True, help="Page size")
@pass_context
def list_ray(
    ctx: Context,
    workspace: Optional[str],
    workspace_id_override: Optional[str],
    all_users: bool,
    created_by: Optional[str],
    page_num: int,
    page_size: int,
) -> None:
    """List Ray (弹性计算) jobs in a workspace."""
    try:
        config, _ = Config.from_files_and_env(require_credentials=False)
        resolved_workspace_id = None
        if workspace is not None or workspace_id_override is not None:
            resolved_workspace_id = select_workspace_id(
                config,
                explicit_workspace_name=workspace,
                explicit_workspace_id=workspace_id_override,
            )

        session = get_web_session()

        user_ids: Optional[list[str]] = None
        if all_users:
            user_ids = None
        elif created_by:
            user_ids = [uid.strip() for uid in created_by.split(",") if uid.strip()]
        else:
            # Default: scope to the logged-in user, mirroring the web UI's
            # "我的" tab so a shared workspace doesn't dump everyone's jobs.
            try:
                me = browser_api_module.get_current_user(session=session)
                current_user_id = str(me.get("id") or me.get("user_id") or "").strip()
                if current_user_id:
                    user_ids = [current_user_id]
            except Exception:
                user_ids = None

        jobs, total = browser_api_module.list_ray_jobs(
            workspace_id=resolved_workspace_id,
            user_ids=user_ids,
            page_num=page_num,
            page_size=page_size,
            session=session,
        )
        rows = [
            {
                "ray_job_id": job.ray_job_id or "N/A",
                "name": job.name or "N/A",
                "status": job.status or "N/A",
                "created_at": job.created_at or "N/A",
                "created_by_name": job.created_by_name or "N/A",
                "created_by_id": job.created_by_id or "",
                "project_name": job.project_name or "",
                "project_id": job.project_id or "",
                "workspace_id": job.workspace_id or "",
            }
            for job in jobs
        ]

        if ctx.json_output:
            click.echo(
                json_formatter.format_json({"jobs": rows, "total": total}),
            )
            return

        click.echo(_format_ray_list_rows(rows))

    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


# ---------------------------------------------------------------------------
# status (detail)
# ---------------------------------------------------------------------------


@click.command("status")
@click.argument("ray_job_id")
@pass_context
def status_ray(ctx: Context, ray_job_id: str) -> None:
    """Show details for a Ray (弹性计算) job.

    The Ray detail payload nests head + worker specs and elastic instance
    ranges; ``--json`` surfaces the full structure, plain output shows the
    top-level status fields.
    """
    try:
        session = get_web_session()
        data = browser_api_module.get_ray_job_detail(ray_job_id, session=session)

        if ctx.json_output:
            click.echo(json_formatter.format_json(data))
            return

        click.echo("Ray Job Status")
        click.echo(f"Ray Job ID: {data.get('ray_job_id') or ray_job_id}")
        click.echo(f"Name:       {data.get('name', 'N/A')}")
        click.echo(f"Status:     {data.get('status', 'N/A')}")
        if data.get("sub_status"):
            click.echo(f"Sub:        {data.get('sub_status')}")
        if data.get("priority") is not None:
            click.echo(f"Priority:   {data.get('priority')}")
        if data.get("priority_level"):
            click.echo(f"Priority Level: {data.get('priority_level')}")
        created_by = data.get("created_by") or {}
        if created_by.get("name"):
            click.echo(f"Created By: {created_by.get('name')}")
        if data.get("project_name"):
            click.echo(f"Project:    {data.get('project_name')}")
        if data.get("created_at"):
            click.echo(f"Created:    {data.get('created_at')}")
        if data.get("finished_at"):
            click.echo(f"Finished:   {data.get('finished_at')}")
        click.echo(
            "\nUse `inspire --json ray status <id>` to see full head / worker "
            "spec and elastic instance ranges."
        )

    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


@click.command("stop")
@click.argument("ray_job_id")
@pass_context
def stop_ray(ctx: Context, ray_job_id: str) -> None:
    """Stop a running Ray (弹性计算) job."""
    try:
        session = get_web_session()
        browser_api_module.stop_ray_job(ray_job_id, session=session)

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"ray_job_id": ray_job_id, "stopped": True},
                )
            )
            return
        click.echo(human_formatter.format_success(f"Ray job stopped: {ray_job_id}"))

    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@click.command("delete")
@click.argument("ray_job_id")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
@pass_context
def delete_ray(ctx: Context, ray_job_id: str, yes: bool) -> None:
    """Permanently delete a Ray (弹性计算) job record.

    \b
    The entry disappears from the web UI. This cannot be undone; if the
    job is still running, `stop` it first so the scheduler releases
    reserved capacity cleanly.
    """
    if not yes and not ctx.json_output:
        click.confirm(
            f"Permanently delete Ray job '{ray_job_id}'? This cannot be undone.",
            abort=True,
        )

    try:
        session = get_web_session()
        browser_api_module.delete_ray_job(ray_job_id, session=session)

        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"ray_job_id": ray_job_id, "status": "deleted"},
                )
            )
            return
        click.echo(human_formatter.format_success(f"Ray job deleted: {ray_job_id}"))

    except (SessionExpiredError, ValueError) as e:
        _handle_error(ctx, "AuthenticationError", str(e), EXIT_AUTH_ERROR)
    except Exception as e:
        _handle_error(ctx, "APIError", str(e), EXIT_API_ERROR)
