"""Delete a named account profile."""

from __future__ import annotations

import click

from inspire.accounts import AccountError
from inspire.services.account.lifecycle import delete_local_account
from inspire.cli.context import Context, EXIT_VALIDATION_ERROR, pass_context
from inspire.services.utils import json_formatter
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
    """Delete a local account profile, including local bridge/tunnel state.

    Platform workloads owned by the account keep running.
    """
    remove_local_account(ctx, name, assume_yes=assume_yes)


def remove_local_account(ctx: Context, name: str, *, assume_yes: bool) -> None:
    """Confirm and delete a profile for both remove and logout."""
    from inspire.cli.commands.login import stdin_is_interactive

    if not assume_yes and not stdin_is_interactive():
        exit_with_error(
            ctx, "ConfirmationRequired", "Account removal requires confirmation.",
            EXIT_VALIDATION_ERROR, hint="Pass --yes to confirm removal.",
        )
    require_confirmation(
        ctx,
        yes=assume_yes,
        prompt=f"Remove account {name!r}?",
        message="Account removal requires confirmation.",
        hint="Pass --yes to confirm removal.",
    )
    try:
        delete_local_account(name)
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
