"""Explicit account device authorization."""

from __future__ import annotations

import json
import sys
import webbrowser

import click

from inspire.accounts import (
    AccountError, account_config_path, account_dir, create_account, current_account,
    default_account, list_accounts, set_current_account, validate_name,
)
from inspire.cli.context import EXIT_VALIDATION_ERROR, Context, pass_context
from inspire.config import DEFAULT_BASE_URL
from inspire.services.account.account_config import render_account_config
from inspire.cli.utils.errors import exit_with_error
from inspire.platform.web.session.auth import device_login
from inspire.platform.web.session.models import WebSession


DEFAULT_ACCOUNT_NAME = "default"
"""Account a bare ``inspire login`` creates when no account exists."""


def run_login(account: str, *, no_browser: bool = False) -> WebSession:
    """Authorize an account while immediately relaying device instructions."""
    def notify(uri: str, code: str, expires: float) -> None:
        click.echo(f"Open {uri}\nCode: {code} (expires in {expires:g} seconds)", err=True)
        sys.stderr.flush()
        if sys.stdout.isatty() and not no_browser:
            try:
                webbrowser.open(uri)
            except Exception:
                pass

    return device_login(account, notify)


def stdin_is_interactive() -> bool:
    return bool(click.get_text_stream("stdin").isatty())


def prompt_account_alias() -> str:
    """Ask for a valid local alias, shared with first-run init."""
    while True:
        name = click.prompt("Account alias", default="", show_default=False, type=str).strip()
        if not name:
            click.echo("Account alias is required.", err=True)
            continue
        try:
            return validate_name(name)
        except AccountError as error:
            click.echo(f"Invalid account alias: {error}", err=True)


def ensure_account_and_login(
    ctx: Context,
    name: str,
    *,
    base_url: str | None = None,
    proxy: str | None = None,
    make_active: bool | None = None,
    non_interactive: bool = False,
    no_login: bool = False,
    no_browser: bool = False,
    creating_only: bool = False,
) -> WebSession | None:
    """Create configuration if needed, choose activation, then authorize."""
    non_interactive = non_interactive or ctx.json_output or not stdin_is_interactive()
    try:
        name = validate_name(name)
        exists = account_dir(name).exists()
        if exists and creating_only:
            raise AccountError(f"Account already exists: {name}")
        if not account_config_path(name).exists():
            if base_url is None:
                base_url = DEFAULT_BASE_URL if non_interactive else click.prompt(
                    "Inspire base URL", default=DEFAULT_BASE_URL, show_default=True,
                )
            if proxy is None:
                proxy = "" if non_interactive else click.prompt(
                    "Proxy URL (leave empty for none)", default="", show_default=False,
                )
            content = render_account_config(base_url=base_url.strip(), proxy=proxy.strip())
            if exists:
                from inspire.services.account.account_config import atomic_write_text
                atomic_write_text(account_config_path(name), content, private=True)
            else:
                create_account(name, content)
        elif base_url is not None or proxy is not None:
            click.echo("Warning: --base-url/--proxy ignored for an existing account.", err=True)
        active = default_account()
        if make_active is None:
            make_active = active is None
            if active is not None and not non_interactive and (creating_only or not exists):
                make_active = click.confirm(
                    f"Current active account is '{active}'. Switch to '{name}'?", default=True,
                )
        if make_active:
            set_current_account(name)
    except AccountError as error:
        exit_with_error(ctx, "AccountError", str(error), EXIT_VALIDATION_ERROR)
    return None if no_login else run_login(name, no_browser=no_browser)


@click.command()
@click.option("--base-url", metavar="URL", help="Base URL for a new account.")
@click.option("--proxy", metavar="URL", help="Proxy URL for a new account; empty means no proxy.")
@click.option("--use/--no-use", "make_active", default=None, help="Set or keep the saved active account.")
@click.option("--no-browser", is_flag=True, help="Do not open a browser automatically.")
@click.option("--json", "json_output", is_flag=True, help="Print login metadata as JSON.")
@pass_context
def login(
    ctx: Context, base_url: str | None, proxy: str | None,
    make_active: bool | None, no_browser: bool, json_output: bool,
) -> None:
    """Create an account if needed and log in with OAuth device authorization."""
    ctx.json_output = ctx.json_output or json_output
    account = current_account()
    if not account:
        if list_accounts():
            raise click.ClickException("No active account. Pass --account NAME or run inspire account use NAME.")
        account = DEFAULT_ACCOUNT_NAME
    try:
        session = ensure_account_and_login(
            ctx, account, base_url=base_url, proxy=proxy, make_active=make_active,
            no_browser=no_browser,
        )
    except ValueError as error:
        raise click.ClickException(str(error)) from None
    assert session is not None
    payload = {
        "account": account,
        "login_username": session.login_username,
        "access_expires_at": session.access_expires_at,
        "refresh_expires_at": session.refresh_expires_at,
    }
    if ctx.json_output:
        click.echo(json.dumps({"success": True, "data": payload}, separators=(",", ":")))
    else:
        click.echo(f"Logged in: {account} ({session.login_username or 'authenticated'})")


@click.command()
@click.option("--yes", "-y", "assume_yes", is_flag=True, help="Confirm local account deletion.")
@pass_context
def logout(ctx: Context, assume_yes: bool) -> None:
    """Delete the local account profile, including local bridge/tunnel state.

    Platform workloads keep running. Equivalent to account remove.
    """
    from inspire.cli.commands.account.remove import remove_local_account

    account = current_account()
    if not account:
        raise click.ClickException("No active account. Pass --account NAME.")
    remove_local_account(ctx, account, assume_yes=assume_yes)
