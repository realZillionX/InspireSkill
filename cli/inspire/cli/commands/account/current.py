"""``inspire account current`` — print the active account name."""

from __future__ import annotations

import sys

import click

from inspire.accounts import current_account


@click.command("current")
def current() -> None:
    """Print the active account name (exits 1 if none is set).

    stdout stays scriptable: ``active=$(inspire account current)`` works.
    The hint when no account is active goes to stderr.
    """
    name = current_account()
    if not name:
        click.echo(
            "No active account. Use 'inspire account use <name>' to set one.",
            err=True,
        )
        sys.exit(1)
    click.echo(name)
