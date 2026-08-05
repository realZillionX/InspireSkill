"""Name-only account management commands."""

import click

from inspire.cli.commands.account.add import add
from inspire.cli.commands.account.current import current
from inspire.cli.commands.account.list_cmd import list_cmd
from inspire.cli.commands.account.remove import remove
from inspire.cli.commands.account.rename import rename
from inspire.cli.commands.account.use import use


@click.group()
def account() -> None:
    """Manage named Inspire account profiles."""


account.add_command(add)
account.add_command(list_cmd)
account.add_command(use)
account.add_command(remove)
account.add_command(rename)
account.add_command(current)
