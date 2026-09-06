"""Set the active account."""

from __future__ import annotations

import click

from inspire.accounts import AccountError, default_account, set_current_account
from inspire.cli.context import Context, EXIT_VALIDATION_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.output import emit_success


@click.command("use")
@click.argument("name", metavar="NAME")
@pass_context
def use(ctx: Context, name: str) -> None:
    """Set the active account for subsequent commands."""
    try:
        set_current_account(name)
    except AccountError as err:
        exit_with_error(ctx, "AccountError", str(err), EXIT_VALIDATION_ERROR)
    active = default_account() or name.strip()
    emit_success(
        ctx,
        payload={"name": active, "status": "selected"},
        text=json_formatter.sanitize_text(
            f"Active account: {active}",
            redact_paths=True,
        ),
    )
