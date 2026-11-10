"""Explicit account device authorization."""

from __future__ import annotations

import json
import sys
import webbrowser

import click

from inspire.accounts import current_account
from inspire.cli.context import Context, pass_context
from inspire.cli.utils.output import emit_success
from inspire.platform.web.session.auth import device_login
from inspire.platform.web.session import clear_session_cache
from inspire.platform.web.session.models import WebSession
from inspire.platform.web.session.refresh_lock import exclusive_session_refresh


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


@click.command()
@click.option("--no-browser", is_flag=True, help="Do not open a browser automatically.")
@click.option("--json", "json_output", is_flag=True, help="Print login metadata as JSON.")
@pass_context
def login(ctx: Context, no_browser: bool, json_output: bool) -> None:
    """Log in using the official OAuth device authorization flow."""
    ctx.json_output = ctx.json_output or json_output
    account = current_account()
    if not account:
        raise click.ClickException("Create an account with inspire account add NAME first.")
    try:
        session = run_login(account, no_browser=no_browser)
    except ValueError as error:
        raise click.ClickException(str(error)) from None
    # Login explicitly exposes the authenticated username. Keep an allowlisted
    # payload rather than serializing the token-bearing session object.
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
@pass_context
def logout(ctx: Context) -> None:
    """Remove the account's cached tokens locally."""
    account = current_account()

    with exclusive_session_refresh(account):
        clear_session_cache(account)
    emit_success(
        ctx, payload={"account": account, "status": "logged_out"}, text="Logged out locally."
    )
