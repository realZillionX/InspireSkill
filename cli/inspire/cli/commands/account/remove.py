"""``inspire account remove <name>`` — delete an account directory."""

from __future__ import annotations

import click

from inspire.accounts import AccountError, remove_account
from inspire.cli.context import Context, EXIT_GENERAL_ERROR, EXIT_VALIDATION_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.output import emit_success


@click.command("remove")
@click.argument("name")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Skip confirmation.")
@pass_context
def remove(ctx: Context, name: str, assume_yes: bool) -> None:
    """Permanently delete an account's local directory.

    Removes ``~/.inspire/accounts/<name>/`` (config.toml, cached notebook
    SSH entries, rtunnel proxy state, login cache). Platform-side resources
    (notebooks, jobs, images) tied to that login keep running — clean them up
    first if needed.
    """
    if not assume_yes:
        if ctx.json_output:
            exit_with_error(
                ctx,
                "ConfirmationRequired",
                "Account removal requires confirmation.",
                EXIT_VALIDATION_ERROR,
                hint="Pass --yes to confirm removal.",
            )
        if not click.confirm(f"Remove account {name!r}?", default=False):
            exit_with_error(
                ctx,
                "Cancelled",
                "Account removal cancelled.",
                EXIT_GENERAL_ERROR,
            )
    try:
        remove_account(name)
    except AccountError as err:
        exit_with_error(ctx, "AccountError", str(err), EXIT_VALIDATION_ERROR)
    normalized_name = name.strip()
    emit_success(
        ctx,
        payload={"name": normalized_name},
        text=json_formatter.sanitize_text(
            f"Removed account: {normalized_name}",
            redact_paths=True,
        ),
    )
