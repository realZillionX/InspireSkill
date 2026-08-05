"""``inspire account current`` — print the active account name."""

from __future__ import annotations

import click

from inspire.accounts import current_account
from inspire.cli.context import Context, EXIT_GENERAL_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.output import emit_success


@click.command("current")
@pass_context
def current(ctx: Context) -> None:
    """Print the active account name; exit 1 when none is set."""
    name = current_account()
    if not name:
        exit_with_error(
            ctx,
            "AccountError",
            "No active account.",
            EXIT_GENERAL_ERROR,
            hint="Use 'inspire account use <name>' to set one.",
        )
    emit_success(
        ctx,
        payload={"name": name},
        text=json_formatter.sanitize_text(name, redact_paths=True),
    )
