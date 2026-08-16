"""Name-only account management commands."""

import click

from inspire.cli.commands.account.add import add
from inspire.cli.commands.account.current import current
from inspire.cli.commands.account.list_cmd import list_cmd
from inspire.cli.commands.account.permissions_cmd import permissions
from inspire.cli.commands.account.remove import remove
from inspire.cli.commands.account.rename import rename
from inspire.cli.commands.account.use import use

# Aliased so the submodules stay reachable as `...account.check` etc.; binding
# the command object to the bare name would shadow the module it came from.
from inspire.cli.commands.account.check import check as check_command
from inspire.cli.commands.account.context import context as context_command
from inspire.cli.commands.account.show import show as show_command


@click.group()
def account() -> None:
    """Manage named Inspire account profiles."""


account.add_command(add)
account.add_command(list_cmd)
account.add_command(use)
account.add_command(remove)
account.add_command(rename)
account.add_command(current)
account.add_command(show_command)
account.add_command(check_command)
account.add_command(context_command)
account.add_command(permissions)
