"""Rename a local account profile."""

from __future__ import annotations

import click

from inspire.accounts import AccountError, current_account, rename_account
from inspire.cli.context import Context, EXIT_VALIDATION_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.output import emit_success


@click.command("rename")
@click.argument("old_name", metavar="OLD_NAME")
@click.argument("new_name", metavar="NEW_NAME")
@pass_context
def rename(ctx: Context, old_name: str, new_name: str) -> None:
    """Rename a local account profile without changing its login."""
    try:
        rename_account(old_name, new_name)
    except AccountError as err:
        exit_with_error(ctx, "AccountError", str(err), EXIT_VALIDATION_ERROR)
    new = new_name.strip()
    active = current_account()
    is_active = active == new
    suffix = " (active)" if is_active else ""
    emit_success(
        ctx,
        payload={"name": new, "status": "renamed", "active": is_active},
        text=json_formatter.sanitize_text(
            f"Account renamed: {new}{suffix}",
            redact_paths=True,
        ),
    )
