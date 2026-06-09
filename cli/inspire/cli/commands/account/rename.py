"""``inspire account rename <old> <new>`` — rename a local account alias."""

from __future__ import annotations

import click

from inspire.accounts import AccountError, current_account, rename_account


@click.command("rename")
@click.argument("old_name")
@click.argument("new_name")
def rename(old_name: str, new_name: str) -> None:
    """Rename a local account alias.

    Moves ``~/.inspire/accounts/<old>`` to ``~/.inspire/accounts/<new>`` and
    updates ``~/.inspire/current`` when the renamed account is active. Platform
    login credentials inside config.toml are preserved; this changes only the
    local alias used by ``inspire account use`` and ``--account``.
    """
    try:
        rename_account(old_name, new_name)
    except AccountError as err:
        raise click.ClickException(str(err)) from err
    click.echo(f"Renamed account: {old_name.strip()} -> {new_name.strip()}")
    active = current_account()
    if active == new_name.strip():
        click.echo(f"Active account: {active}")
