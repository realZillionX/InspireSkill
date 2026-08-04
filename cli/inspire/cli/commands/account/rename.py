"""``inspire account rename <old> <new>`` — rename a local account alias."""

from __future__ import annotations

import click

from inspire.accounts import AccountError, current_account, rename_account
from inspire.cli.context import Context, EXIT_VALIDATION_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.output import emit_success


@click.command("rename")
@click.argument("old_name")
@click.argument("new_name")
@pass_context
def rename(ctx: Context, old_name: str, new_name: str) -> None:
    """Rename a local account alias.

    Moves ``~/.inspire/accounts/<old>`` to ``~/.inspire/accounts/<new>`` and
    updates ``~/.inspire/current`` when the renamed account is active. Platform
    login credentials inside config.toml are preserved; this changes only the
    local alias used by ``inspire account use`` and ``--account``.
    """
    try:
        rename_account(old_name, new_name)
    except AccountError as err:
        exit_with_error(ctx, "AccountError", str(err), EXIT_VALIDATION_ERROR)
    old = old_name.strip()
    new = new_name.strip()
    active = current_account()
    is_active = active == new
    suffix = " (active)" if is_active else ""
    emit_success(
        ctx,
        payload={"old_name": old, "name": new, "active": is_active},
        text=json_formatter.sanitize_text(
            f"Renamed account: {old} -> {new}{suffix}",
            redact_paths=True,
        ),
    )
