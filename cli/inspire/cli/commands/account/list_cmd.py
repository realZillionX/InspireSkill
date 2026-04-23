"""``inspire account list`` — list all accounts, marking the active one."""

from __future__ import annotations

import click

from inspire.accounts import current_account, list_accounts


@click.command("list")
def list_cmd() -> None:
    """List all configured accounts. Active account is marked with ``*``."""
    names = list_accounts()
    if not names:
        click.echo(
            "No accounts configured. Use 'inspire account add <name>' to create one."
        )
        return
    active = current_account()
    for name in names:
        marker = "*" if name == active else " "
        click.echo(f" {marker} {name}")
