"""Implementation for the account-only ``inspire init`` command."""

from __future__ import annotations

from pathlib import Path

import click

from inspire.accounts import (
    AccountError,
    account_scope,
    create_account,
    current_account,
    ensure_inspire_home,
    list_accounts,
    set_current_account,
)
from inspire.services.account.account_config import render_account_config as _render_account_config
from inspire.cli.context import Context, EXIT_GENERAL_ERROR, pass_context
from inspire.cli.utils.errors import exit_with_error as _handle_error
from inspire.config import DEFAULT_BASE_URL, Config

from .discover import _init_discover_mode
from .env_detect import _detect_env_vars
from .errors import run_init_action
from .json_report import emit_init_result, snapshot_paths
from .templates import _init_smart_mode, _init_template_mode


_NO_ACTIVE_ACCOUNT_MESSAGE = "No active account configured. Run `inspire account add` first."


def _stdin_is_interactive() -> bool:
    stream = click.get_text_stream("stdin")
    return bool(getattr(stream, "isatty", lambda: False)())


def _require_active_account_config_path() -> Path:
    path = Config.writable_config_path()
    if path is None:
        raise ValueError(_NO_ACTIVE_ACCOUNT_MESSAGE)
    return path


def _bootstrap_first_account_if_needed(
    *,
    effective_json: bool,
    non_interactive: bool,
    cli_base_url: str | None,
) -> bool:
    """Create the first account inline for an interactive first run."""
    if current_account():
        return False
    if list_accounts():
        raise ValueError(
            "No active account configured. Run `inspire account use <name>` first."
        )
    if effective_json or non_interactive:
        raise ValueError(
            "No active account configured. Run `inspire account add <name>` first; "
            "non-interactive init requires an account alias."
        )

    ensure_inspire_home()
    from inspire.cli.commands.login import prompt_account_alias
    account_name = prompt_account_alias()

    base_url = (
        click.prompt("Inspire base URL", default=DEFAULT_BASE_URL, show_default=True)
        if cli_base_url is None
        else cli_base_url
    )
    proxy = click.prompt("Proxy URL (leave empty for none)", default="", show_default=False)

    content = _render_account_config(
        base_url=base_url.strip(),
        proxy=(proxy or "").strip(),
    )
    try:
        create_account(account_name, content)
        set_current_account(account_name)
        click.get_current_context().with_resource(account_scope(account_name))
    except AccountError as err:
        raise ValueError(str(err)) from err

    click.echo(f"Active account: {account_name}")
    from inspire.cli.commands.login import run_login
    run_login(account_name)
    return True


def _login_if_logged_out() -> None:
    """Start device login before discovery captures output in JSON mode."""
    from inspire.platform.web.session import auth
    from inspire.platform.web.session.models import AuthenticationError

    account = current_account()
    if not account:
        return
    try:
        auth.get_web_session(account=account)
    except AuthenticationError:
        from inspire.cli.commands.login import run_login

        click.echo(f"Account {account} is not logged in; starting device login.", err=True)
        run_login(account)


@click.command()
@click.option("--force", "-f", is_flag=True, help="Overwrite existing config without prompting")
@click.option(
    "--template",
    "-t",
    "template_flag",
    is_flag=True,
    help="Create an account template with placeholders (skip discovery)",
)
@click.option(
    "--no-discover",
    is_flag=True,
    help="Skip platform discovery and initialize from environment variables.",
)
@click.option(
    "--username",
    "-u",
    default=None,
    metavar="LOGIN",
    hidden=True,
    help="Deprecated and ignored.",
)
@click.option(
    "--base-url",
    default=None,
    metavar="URL",
    help="Platform base URL used during first-account discovery.",
)
@pass_context
def init(
    ctx: Context,
    force: bool,
    template_flag: bool,
    no_discover: bool,
    username: str | None,
    base_url: str | None,
) -> None:
    """Validate and normalize the active account configuration.

    The command writes only ``~/.inspire/accounts/<account>/config.toml``.
    Project, workspace, group, quota, image and remote paths remain explicit
    command inputs or live platform data; no repository-local config is read
    or created.

    \b
    Examples:
      inspire init
      inspire init --force
      inspire init --template --force
      inspire init --no-discover --force
    """
    if username is not None:
        click.echo("Deprecated: --username is ignored; use device login.", err=True)
    effective_json = ctx.json_output
    non_interactive = effective_json or not _stdin_is_interactive()
    warnings: list[str] = []
    run_discovery = not template_flag and not no_discover

    def _warn(message: str) -> None:
        warnings.append(message)
        if not effective_json:
            click.echo(click.style(f"Warning: {message}", fg="yellow"))

    try:
        if run_discovery:
            _bootstrap_first_account_if_needed(
                effective_json=effective_json,
                non_interactive=non_interactive,
                cli_base_url=base_url,
            )

        account_path = _require_active_account_config_path()
        before = snapshot_paths(account_path)

        if not run_discovery and base_url:
            _warn("--base-url only applies to discovery and was ignored.")

        if non_interactive and account_path.exists() and not force:
            raise ValueError(
                "Non-interactive init requires --force when account config already exists."
            )

        if run_discovery:
            _login_if_logged_out()
            run_init_action(
                _init_discover_mode,
                effective_json,
                force,
                cli_base_url=base_url,
                non_interactive=non_interactive,
            )
        elif template_flag:
            run_init_action(_init_template_mode, effective_json, force)
        else:
            detected = _detect_env_vars()
            if detected:
                run_init_action(_init_smart_mode, effective_json, detected, force)
            else:
                run_init_action(_init_template_mode, effective_json, force)

        emit_init_result(
            target_paths=[account_path],
            before=before,
            warnings=warnings,
            effective_json=effective_json,
        )
    except ValueError as err:
        _handle_error(ctx, "ValidationError", str(err), EXIT_GENERAL_ERROR)
    except SystemExit:
        raise
    except Exception as err:
        _handle_error(ctx, "Error", str(err), EXIT_GENERAL_ERROR)


__all__ = ["init"]
