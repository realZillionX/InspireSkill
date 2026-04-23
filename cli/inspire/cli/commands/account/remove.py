"""``inspire account remove <name>`` — delete an account directory."""

from __future__ import annotations

import click

from inspire.accounts import AccountError, remove_account


@click.command("remove")
@click.argument("name")
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Skip confirmation.")
def remove(name: str, assume_yes: bool) -> None:
    """Permanently delete an account's local directory.

    Removes ``~/.inspire/accounts/<name>/`` (config.toml, tunnel bridges,
    session cache, rtunnel cache). Platform-side resources (notebooks, jobs,
    images) tied to that login keep running — clean them up first if needed.
    """
    if not assume_yes:
        click.confirm(
            f"Delete account {name!r} and all its local files "
            "(config.toml, SSH tunnel bridges, session cache)?",
            abort=True,
        )
    try:
        remove_account(name)
    except AccountError as err:
        raise click.ClickException(str(err)) from err
    click.echo(f"Removed account: {name}")
