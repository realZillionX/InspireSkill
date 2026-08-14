"""`inspire job tensorboards` — the TensorBoards the platform runs for training jobs.

The platform starts a TensorBoard as its own object beside the training job, and
until now the CLI could not see them at all. What makes this worth a command is
not the web address — an Agent has no browser, and this CLI deliberately dropped
the "open a page" commands — but `Summary Path`: the shared-disk directory the
event files land in. That path is readable from any notebook in the same project,
so it turns "the platform has a TensorBoard for this run" into something an Agent
can actually act on.
"""

from __future__ import annotations

from typing import Any, Optional

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
    DEFAULT_COLLECTION_LIMIT,
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config, ConfigError
from inspire.config.workspaces import select_workspace_id
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import SessionExpiredError, get_web_session


def _row(board: Any) -> dict[str, str]:
    return {
        "name": scrub_raw_ids(board.name),
        "status": scrub_raw_ids(board.status),
        "job": scrub_raw_ids(board.job_name),
        "summary_path": scrub_raw_ids(board.summary_path),
    }


@click.command("tensorboards")
@click.option("--workspace", required=True, metavar="NAME", help="Workspace name.")
@click.option(
    "--limit",
    "-n",
    "limit",
    type=click.IntRange(1),
    default=None,
    help=f"Maximum rows to print (default: {DEFAULT_COLLECTION_LIMIT}).",
)
@click.option("--all", "show_all", is_flag=True, help="Print every TensorBoard.")
@pass_context
def job_tensorboards(
    ctx: Context,
    workspace: str,
    limit: Optional[int],
    show_all: bool,
) -> None:
    """List the TensorBoards the platform runs for this account's training jobs.

    `Summary Path` is the shared-disk directory the event files are written to.
    It is the field to act on: read it from a notebook in the same project
    instead of opening the platform's web view.

    \b
    Examples:
        inspire job tensorboards --workspace 分布式训练空间
        inspire job tensorboards --workspace 分布式训练空间 --all
    """
    try:
        effective_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as e:
        _handle_error(ctx, "ValidationError", str(e), EXIT_VALIDATION_ERROR)
        return

    request_limit = (
        effective_limit if effective_limit is not None else DEFAULT_COLLECTION_LIMIT
    )

    try:
        Config.from_files_and_env(require_credentials=False)
        session = get_web_session()
        workspace_id = select_workspace_id(
            explicit_workspace_name=workspace,
            session=session,
        )
        if workspace_id is None:
            raise ConfigError("--workspace is required.")

        boards, total = browser_api_module.list_tensorboards(
            workspace_id=workspace_id,
            page_num=1,
            page_size=request_limit,
            session=session,
        )
        if show_all and total > len(boards):
            boards, total = browser_api_module.list_tensorboards(
                workspace_id=workspace_id,
                page_num=1,
                page_size=total,
                session=session,
            )
    except ConfigError as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return
    except SessionExpiredError as e:
        _handle_error(ctx, "AuthenticationError", scrub_raw_ids(e), EXIT_AUTH_ERROR)
        return
    except Exception as e:
        _handle_error(ctx, "APIError", scrub_raw_ids(e), EXIT_API_ERROR)
        return

    page = bound_collection([_row(b) for b in boards], limit=effective_limit, total=total)
    if ctx.json_output:
        click.echo(json_formatter.format_json({"items": page.items, **page.metadata()}))
        return

    if not page.items:
        click.echo("No TensorBoards found.")
        return

    headers = ["Name", "Status", "Job", "Summary Path"]
    keys = ["name", "status", "job", "summary_path"]
    widths = [
        column_width(header, [row[key] for row in page.items])
        for header, key in zip(headers, keys)
    ]
    values = [[row[key] for key in keys] for row in page.items]
    click.echo("\n".join(render_table(headers, values, widths, line_char="─")))
    notice = truncation_notice(page)
    if notice:
        click.echo(notice)


__all__ = ["job_tensorboards"]
