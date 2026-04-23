"""``inspire account use <name>`` — switch the active account."""

from __future__ import annotations

import click

from inspire.accounts import AccountError, set_current_account


@click.command("use")
@click.argument("name")
def use(name: str) -> None:
    """Switch the active account.

    Updates ``~/.inspire/current`` so every subsequent ``inspire`` command
    resolves its config, SSH tunnel aliases, and SSO session cache under
    ``~/.inspire/accounts/<name>/``.
    """
    try:
        set_current_account(name)
    except AccountError as err:
        raise click.ClickException(str(err)) from err
    click.echo(f"Active account: {name}")
