"""Explicit account device authorization."""

from __future__ import annotations

import json
import sys
import webbrowser
from collections.abc import Callable
from pathlib import Path

import click

from inspire.accounts import (
    AccountError,
    account_config_path,
    account_dir,
    create_account,
    current_account,
    default_account,
    list_accounts,
    set_current_account,
    validate_name,
)
from inspire.cli.context import EXIT_AUTH_ERROR, EXIT_VALIDATION_ERROR, Context, pass_context
from inspire.config import DEFAULT_BASE_URL
from inspire.services.account.account_config import render_account_config
from inspire.cli.utils.errors import exit_with_error
from inspire.platform.web.session.auth import device_login
from inspire.platform.web.session.models import AuthenticationError, WebSession
from inspire.platform.web.session import cas
from inspire.platform.web.session.auth import _load_runtime_config
from inspire.platform.web.session.cas_qr import render_qr


DEFAULT_ACCOUNT_NAME = "default"
"""Account a bare ``inspire login`` creates when no account exists."""


@cas.login_flow
def run_login(
    account: str, *, no_browser: bool = False, cas_metadata: dict | None = None
) -> WebSession:
    """Authorize an account while immediately relaying device instructions."""

    def notify(uri: str, code: str, expires: float) -> None:
        click.echo(f"Open {uri}\nCode: {code} (expires in {expires:g} seconds)", err=True)
        sys.stderr.flush()
        if sys.stdout.isatty() and not no_browser:
            try:
                webbrowser.open(uri)
            except Exception:
                pass

    stored = cas.CasSession.load(account)
    valid = stored is not None and cas.cas_ticket_valid(stored, account)
    if cas_metadata is not None:
        cas_metadata.update(cas=valid, cas_method=stored.method if valid and stored else None)
    if valid and stored:
        base_url = _load_runtime_config(account).base_url
        with stored.http(account) as http:

            def approve(uri: str, code: str, expires: float) -> None:
                if not cas.approve_device(http, uri, base_url):
                    notify(uri, code, expires)

            return device_login(account, approve)
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
    login_runner: Callable[[str], WebSession | None] | None = None,
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
                base_url = (
                    DEFAULT_BASE_URL
                    if non_interactive
                    else click.prompt(
                        "Inspire base URL",
                        default=DEFAULT_BASE_URL,
                        show_default=True,
                    )
                )
            if proxy is None:
                proxy = (
                    ""
                    if non_interactive
                    else click.prompt(
                        "Proxy URL (leave empty for none)",
                        default="",
                        show_default=False,
                    )
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
                    f"Current active account is '{active}'. Switch to '{name}'?",
                    default=True,
                )
        if make_active:
            set_current_account(name)
    except AccountError as error:
        exit_with_error(ctx, "AccountError", str(error), EXIT_VALIDATION_ERROR)
    if no_login:
        return None
    with cas.account_login_flow(name):
        if login_runner is not None:
            return login_runner(name)
        return run_login(name, no_browser=no_browser)


@cas.login_flow
def _cas_login(
    account: str, method: str, phone: str | None, sms_code: str | None, interactive: bool
) -> bool:
    """Return false after the noninteractive send-only step."""
    if method == "feishu":

        def show(path: Path) -> None:
            click.echo(f"Feishu QR PNG: {path}", err=True)
            rendered = render_qr(path)
            if rendered:
                click.echo(rendered, err=True, color=True)

        session = cas.feishu_qr_login(show, account)
    else:
        if not phone:
            if not interactive:
                raise cas.CasError("--phone NUMBER is required for SMS login.")
            phone = click.prompt("Mobile phone number", err=True)
        cas.validate_phone(phone)
        pending = cas.SmsPending.load(account) if sms_code else None
        with cas.build_cas_http(pending.cookies if pending else [], account) as http:
            if pending is None:
                pending = cas.sms_send_code(phone, http)
                pending.save(account)
                if not interactive:
                    click.echo(
                        "SMS sent. Complete with inspire login --cas sms --phone NUMBER --sms-code CODE (use the same account).",
                        err=True,
                    )
                    return False
            for attempt in range(3):
                code = sms_code or click.prompt("SMS code", err=True)
                try:
                    session = cas.sms_complete(phone, code, pending, http)
                    break
                except cas.SmsCodeRejected:
                    pending.save(account, update=True)
                    if sms_code or not interactive or attempt == 2:
                        raise
                    click.echo("SMS code was not accepted; try again.", err=True)
        pending.delete(account)
    session.save(account)
    return True


@click.command()
@click.option(
    "--cas",
    "cas_method",
    type=click.Choice(["sms", "feishu", "choose"]),
    is_flag=False,
    flag_value="choose",
    default=None,
    metavar="[sms|feishu]",
    help="CAS login via sms or feishu; bare --cas prompts for a method.",
)
@click.option("--phone", help="Mobile number for CAS SMS login (never saved).")
@click.option("--sms-code", help="Complete a pending CAS SMS login (never saved).")
@click.option("--base-url", metavar="URL", help="Base URL for a new account.")
@click.option("--proxy", metavar="URL", help="Proxy URL for a new account; empty means no proxy.")
@click.option(
    "--use/--no-use", "make_active", default=None, help="Set or keep the saved active account."
)
@click.option("--no-browser", is_flag=True, help="Do not open a browser automatically.")
@click.option("--json", "json_output", is_flag=True, help="Print login metadata as JSON.")
@pass_context
def login(
    ctx: Context,
    base_url: str | None,
    proxy: str | None,
    make_active: bool | None,
    no_browser: bool,
    json_output: bool,
    cas_method: str | None,
    phone: str | None,
    sms_code: str | None,
) -> None:
    """Log in with OAuth device authorization, optionally using CAS for data plaza access."""
    ctx.json_output = ctx.json_output or json_output
    interactive = not ctx.json_output and stdin_is_interactive()
    if cas_method == "choose":
        if not interactive:
            exit_with_error(
                ctx,
                "ValidationError",
                "Specify --cas sms or --cas feishu in non-interactive/JSON mode.",
                EXIT_VALIDATION_ERROR,
                json_to_stdout=True,
            )
        cas_method = click.prompt(
            "CAS login method", type=click.Choice(["sms", "feishu"]), default="feishu", err=True
        )
    if (phone is not None or sms_code is not None) and cas_method != "sms":
        exit_with_error(
            ctx, "ValidationError", "--phone and --sms-code require --cas sms.",
            EXIT_VALIDATION_ERROR, json_to_stdout=True,
        )
    if cas_method == "sms":
        if not phone and not interactive:
            exit_with_error(
                ctx, "ValidationError", "--phone NUMBER is required for SMS login.",
                EXIT_VALIDATION_ERROR, json_to_stdout=True,
            )
        if phone:
            try:
                cas.validate_phone(phone)
            except cas.CasError as error:
                exit_with_error(
                    ctx, "ValidationError", str(error), EXIT_VALIDATION_ERROR, json_to_stdout=True
                )
    metadata: dict = {"cas": False, "cas_method": None}

    def runner(name: str) -> WebSession | None:
        if cas_method and not _cas_login(name, cas_method, phone, sms_code, interactive):
            return None
        return run_login(name, no_browser=no_browser or bool(cas_method), cas_metadata=metadata)

    account = current_account()
    if not account:
        if list_accounts():
            raise click.ClickException(
                "No active account. Pass --account NAME or run inspire account use NAME."
            )
        account = DEFAULT_ACCOUNT_NAME
    try:
        session = ensure_account_and_login(
            ctx,
            account,
            base_url=base_url,
            proxy=proxy,
            make_active=make_active,
            no_browser=no_browser,
            login_runner=runner,
        )
    except cas.CasValidationError as error:
        exit_with_error(ctx, "ValidationError", str(error), EXIT_VALIDATION_ERROR, json_to_stdout=True)
    except AuthenticationError as error:
        exit_with_error(ctx, "AuthenticationError", str(error), EXIT_AUTH_ERROR, json_to_stdout=True)
    except ValueError as error:
        exit_with_error(ctx, "ValidationError", str(error), EXIT_VALIDATION_ERROR, json_to_stdout=True)
    if session is None:
        if ctx.json_output:
            click.echo(
                json.dumps(
                    {"success": True, "data": {"account": account, "pending": True, **metadata}}
                )
            )
        return
    payload = {
        **metadata,
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
