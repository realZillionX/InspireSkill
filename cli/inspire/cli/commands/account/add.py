"""Create a named account profile."""

from __future__ import annotations


import click

from inspire.accounts import (
    AccountError,
    account_dir,
    create_account,
    default_account,
    ensure_inspire_home,
    set_current_account,
    validate_name,
)
from inspire.cli.context import Context, EXIT_VALIDATION_ERROR, pass_context
from inspire.services.utils import json_formatter
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.output import emit_success
from inspire.config import DEFAULT_BASE_URL
from inspire.services.account.account_config import render_account_config as _render_config


@click.command("add")
@click.argument("name", metavar="NAME")
@click.option(
    "--username",
    metavar="LOGIN",
    hidden=True,
    help=(
        "Platform login username, such as a phone number, student number, "
        "or email. Defaults to the account name."
    ),
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

    Use --no-login to create only the local profile.
    """
    non_interactive = non_interactive or ctx.json_output

    try:
        validated = validate_name(name)
    except AccountError as err:
        exit_with_error(ctx, "AccountError", str(err), EXIT_VALIDATION_ERROR)

    ensure_inspire_home()
    if account_dir(validated).exists():
        exit_with_error(
            ctx,
            "AccountError",
            f"Account already exists: {validated}",
            EXIT_VALIDATION_ERROR,
        )

    if username is not None or password is not None:
        click.echo("Deprecated: --username and --password are ignored; use device login.", err=True)
    if base_url is None:
        if non_interactive:
            base_url = DEFAULT_BASE_URL
        else:
            base_url = click.prompt(
                "Inspire base URL",
                default=DEFAULT_BASE_URL,
                show_default=True,
            )

    if proxy is None:
        if non_interactive:
            proxy = ""
        else:
            proxy = click.prompt(
                "Proxy URL (leave empty for none)",
                default="",
                show_default=False,
            )

    content = _render_config(
        username="",
        password="",
        base_url=base_url.strip(),
        proxy=(proxy or "").strip(),
    )

    try:
        create_account(validated, content)
    except AccountError as err:
        exit_with_error(ctx, "AccountError", str(err), EXIT_VALIDATION_ERROR)

    existing_active = default_account()
    if make_active is None:
        if existing_active is None:
            make_active = True  # first account always activates
        elif non_interactive:
            make_active = False
        else:
            make_active = click.confirm(
                f"Current active account is '{existing_active}'. Switch to '{validated}'?",
                default=True,
            )

    if make_active:
        set_current_account(validated)

    if not no_login:
        from inspire.cli.commands.login import run_login

        run_login(validated)

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
