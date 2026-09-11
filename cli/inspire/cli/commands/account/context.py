"""Name-only view of the active account context."""

from __future__ import annotations

from inspire.services.account.account_context import (
    collect_context as _collect_context,
    bound_context,
)

from typing import Any

import click

from inspire.cli.context import (
    Context,
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_VALIDATION_ERROR,
    pass_context,
)
from inspire.services.utils import json_formatter
from inspire.services.utils.collections import (
    resolve_collection_limit,
)
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.config import Config, ConfigError


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

    data = bound_context(_collect_context(cfg), effective_limit)

    if ctx.json_output:
        click.echo(json_formatter.format_json(data))
        return

    _render_human(data)
