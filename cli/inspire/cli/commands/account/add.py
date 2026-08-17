"""Create a named account profile."""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout

import click

from inspire.accounts import (
    AccountError,
    account_dir,
    create_account,
    current_account,
    ensure_inspire_home,
    set_current_account,
    validate_name,
)
from inspire.cli.context import Context, EXIT_VALIDATION_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.output import emit_success
from inspire.config import DEFAULT_BASE_URL


@click.command("add")
@click.argument("name", metavar="NAME")
@click.option(
    "--username",
    metavar="LOGIN",
    help=(
        "Platform login username, such as a phone number, student number, "
        "or email. Defaults to the account name."
    ),
)
@click.option(
    "--password",
    metavar="PASSWORD",
    help="Platform password. Asked interactively (with confirmation) if omitted.",
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
    help="Skip all prompts. Missing fields fall back to defaults; missing --password aborts.",
)
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
) -> None:
    """Create a named account profile.

    Missing login fields are prompted for unless ``--non-interactive`` is set.

    \b
    Examples:
        inspire account add alice
        inspire account add alice \\
          --username user-abc123 --password "$INSPIRE_PW" \\
          --use --non-interactive
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

    if username is None:
        if non_interactive:
            username = validated
        else:
            username = click.prompt(
                "Platform login username (not display name)",
                default=validated,
                show_default=True,
            )
    resolved_username = username.strip()
    if not resolved_username:
        exit_with_error(
            ctx,
            "AccountError",
            "Username cannot be empty.",
            EXIT_VALIDATION_ERROR,
        )

    if password is None:
        if non_interactive:
            exit_with_error(
                ctx,
                "AccountError",
                "--password is required in non-interactive mode.",
                EXIT_VALIDATION_ERROR,
            )
        password = click.prompt(
            "Platform password",
            hide_input=True,
            confirmation_prompt="Confirm password",
        )

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
        username=resolved_username,
        password=password,
        base_url=base_url.strip(),
        proxy=(proxy or "").strip(),
    )

    try:
        create_account(validated, content)
    except AccountError as err:
        exit_with_error(ctx, "AccountError", str(err), EXIT_VALIDATION_ERROR)

    existing_active = current_account()
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

    # Check the browser runtime required by web-side commands. This is
    # best-effort so account creation still succeeds when Playwright setup is
    # deferred.
    from inspire.accounts import normalize_environment

    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        normalize_environment(
            interactive=not non_interactive,
            auto_install_playwright=not non_interactive,
        )

    is_active = current_account() == validated
    suffix = " (active)" if is_active else ""
    emit_success(
        ctx,
        payload={"name": validated, "status": "created", "active": is_active},
        text=json_formatter.sanitize_text(
            f"Account added: {validated}{suffix}",
            redact_paths=True,
        ),
    )


def _toml_basic(s: str) -> str:
    """Escape a string for a TOML basic (double-quoted) string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _render_config(*, username: str, password: str, base_url: str, proxy: str) -> str:
    """Write a minimal account config.toml using the real schema section names.

    Keys must live under [auth]/[api]/[proxy] — the loader resolves
    ``auth.username`` / ``api.base_url`` etc. via the flattened TOML path,
    and a bare top-level ``username = "..."`` silently fails to bind.
    """
    lines = [
        "[auth]",
        f'username = "{_toml_basic(username)}"',
        f'password = "{_toml_basic(password)}"',
        "",
        "[api]",
        f'base_url = "{_toml_basic(base_url)}"',
    ]
    if proxy:
        escaped = _toml_basic(proxy)
        lines.extend(
            [
                "",
                "[proxy]",
                f'requests_http = "{escaped}"',
                f'requests_https = "{escaped}"',
                f'playwright = "{escaped}"',
                f'rtunnel = "{escaped}"',
            ]
        )
    return "\n".join(lines) + "\n"
