"""Create a named account profile."""

from __future__ import annotations


import click

from inspire.accounts import default_account
from inspire.cli.context import Context, pass_context
from inspire.services.utils import json_formatter
from inspire.cli.utils.output import emit_success
from inspire.config import DEFAULT_BASE_URL


@click.command("add")
@click.argument("name", metavar="NAME")
@click.option(
    "--username",
    metavar="LOGIN",
    hidden=True,
    help="Deprecated and ignored.",
)
@click.option(
    "--password",
    metavar="PASSWORD",
    hidden=True,
    help="Deprecated and ignored.",
)
@click.option(
    "--base-url",
    metavar="URL",
    help=f"Inspire platform base URL. Asked interactively if omitted. Default: {DEFAULT_BASE_URL}",
)
@click.option(
    "--proxy",
    metavar="URL",
    help="HTTP/SOCKS5 proxy URL. Pass an empty string to use no proxy.",
)
@click.option(
    "--use/--no-use",
    "make_active",
    default=None,
    help="Set as active after creation. Asked interactively if omitted and another account is active.",
)
@click.option(
    "--non-interactive",
    is_flag=True,
    help="Skip all prompts. Missing fields use defaults; device authorization still waits.",
)
@click.option("--no-login", is_flag=True, help="Create the profile without logging in.")
@pass_context
def add(
    ctx: Context,
    name: str,
    username: str | None,
    password: str | None,
    base_url: str | None,
    proxy: str | None,
    make_active: bool | None,
    non_interactive: bool,
    no_login: bool,
) -> None:
    """Create an account and authorize it with the official device flow.

    Compatible creation entry point for login --account NAME; existing names fail.
    Use --no-login to create only the local profile.
    """
    from inspire.cli.commands.login import ensure_account_and_login

    if username is not None or password is not None:
        click.echo("Deprecated: --username and --password are ignored; use device login.", err=True)
    ensure_account_and_login(
        ctx, name, base_url=base_url, proxy=proxy, make_active=make_active,
        non_interactive=non_interactive, no_login=no_login, creating_only=True,
    )
    validated = name.strip()
    is_active = default_account() == validated
    suffix = " (active)" if is_active else ""
    emit_success(
        ctx,
        payload={"name": validated, "status": "created", "active": is_active},
        text=json_formatter.sanitize_text(
            f"Account added: {validated}{suffix}",
            redact_paths=True,
        ),
    )
