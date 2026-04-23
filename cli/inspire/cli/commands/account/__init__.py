"""``inspire account`` — simple multi-account management.

One account = one isolated directory under ``~/.inspire/accounts/<name>/``.
Switch the active account with a single-line pointer at ``~/.inspire/current``.
No layered merge, no env-var precedence chain.
"""

import click

from inspire.cli.commands.account.add import add
from inspire.cli.commands.account.current import current
from inspire.cli.commands.account.list_cmd import list_cmd
from inspire.cli.commands.account.remove import remove
from inspire.cli.commands.account.use import use


@click.group()
def account() -> None:
    """Manage Inspire accounts.

    Each account lives in its own directory under
    ``~/.inspire/accounts/<name>/`` with its own config.toml, SSH tunnel
    bridges, and SSO session cache. Switch the active account with
    ``inspire account use <name>``; inspect with ``inspire account list``.
    """


account.add_command(add)
account.add_command(list_cmd)
account.add_command(use)
account.add_command(remove)
account.add_command(current)
