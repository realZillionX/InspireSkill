"""Delete a named account profile."""

from __future__ import annotations

import click

from inspire.accounts import AccountError, remove_account
from inspire.cli.context import Context, EXIT_VALIDATION_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error, require_confirmation
from inspire.cli.utils.output import emit_success


@click.command("remove")
@click.argument("name", metavar="NAME")
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
@pass_context
def remove(ctx: Context, name: str, assume_yes: bool) -> None:
    """Delete a local account profile.

    Platform workloads owned by the account keep running.
    """
    require_confirmation(
        ctx,
        yes=assume_yes,
        prompt=f"Remove account {name!r}?",
        message="Account removal requires confirmation.",
        hint="Pass --yes to confirm removal.",
    )
    try:
        remove_account(name)
    except AccountError as err:
        exit_with_error(ctx, "AccountError", str(err), EXIT_VALIDATION_ERROR)
    normalized_name = name.strip()
    emit_success(
        ctx,
        payload={"name": normalized_name, "status": "deleted"},
        text=json_formatter.sanitize_text(
            f"Removed account: {normalized_name}",
            redact_paths=True,
        ),
    )
