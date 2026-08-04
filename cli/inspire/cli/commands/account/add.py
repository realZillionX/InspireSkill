"""``inspire account add <name>`` — create a new account directory."""

from __future__ import annotations

import io
import logging
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

DEFAULT_BASE_URL = "https://qz.sii.edu.cn"
EXAMPLE_PROXY = "http://127.0.0.1:7897"
logger = logging.getLogger(__name__)


@click.command("add")
@click.argument("name")
@click.option(
    "--username",
    help=(
        "Platform login username, such as phone, student ID, or email "
        "(not the display name). Defaults to the account name; asked "
        "interactively if omitted."
    ),
)
@click.option(
    "--password",
    help="Platform password. Asked interactively (with confirmation) if omitted.",
)
@click.option(
    "--base-url",
    help=f"Inspire platform base URL. Asked interactively if omitted. Default: {DEFAULT_BASE_URL}",
)
@click.option(
    "--proxy",
    help="HTTP/SOCKS5 proxy for both public internet and *.sii.edu.cn. "
    "Asked interactively if omitted; pass empty string to skip.",
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
    """Create a new account at ``~/.inspire/accounts/<name>/``.

    By default walks you through five short prompts — platform login username,
    password (with confirmation), base URL, proxy, and whether to switch
    to the new account. Any value passed via a flag skips the matching
    prompt. Pass ``--non-interactive`` to silence every prompt; missing
    fields fall back to defaults, and a missing ``--password`` aborts.

    \b
    Examples:
        # Interactive (recommended for first-time setup):
        inspire account add alice

        # Fully scripted (CI, automation):
        # Replace 7897 with your local Clash mixed port when needed.
        inspire account add alice \\
          --username user-abc123 --password "$INSPIRE_PW" \\
          --proxy http://127.0.0.1:7897 --use --non-interactive
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

    # ---- username -------------------------------------------------------
    if username is None:
        if non_interactive:
            username = validated
        else:
            username = click.prompt(
                "Platform login username (login ID, not display name)",
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

    # ---- password -------------------------------------------------------
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

    # ---- base URL -------------------------------------------------------
    if base_url is None:
        if non_interactive:
            base_url = DEFAULT_BASE_URL
        else:
            base_url = click.prompt(
                "Inspire base URL",
                default=DEFAULT_BASE_URL,
                show_default=True,
            )

    # ---- proxy ----------------------------------------------------------
    if proxy is None:
        if non_interactive:
            proxy = ""
        else:
            click.echo(
                "Proxy must reach BOTH the public internet and *.sii.edu.cn. "
                f"Example if your Clash mixed port is 7897: {EXAMPLE_PROXY}"
            )
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

    # ---- active-account decision ---------------------------------------
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

    # Normalize the wider environment once — quarantine pre-v3 unscoped files,
    # warn on stale env vars dropped by v3.x, ensure playwright is ready for
    # the SSO login that every web-side command needs. Idempotent via the
    # ~/.inspire/.environment-normalized-v3 sentinel; subsequent `account add`
    # invocations are silent when the environment is already clean.
    from inspire.accounts import normalize_environment

    normalization_stdout = io.StringIO()
    normalization_stderr = io.StringIO()
    with redirect_stdout(normalization_stdout), redirect_stderr(normalization_stderr):
        report = normalize_environment(
            interactive=not non_interactive,
            auto_install_playwright=not non_interactive,
        )
    if captured := normalization_stdout.getvalue().strip():
        logger.debug("Suppressed normalization stdout: %s", captured)
    if captured := normalization_stderr.getvalue().strip():
        logger.debug("Suppressed normalization stderr: %s", captured)
    logger.debug("Account environment normalization result: %r", report)

    is_active = current_account() == validated
    suffix = " (active)" if is_active else ""
    emit_success(
        ctx,
        payload={"name": validated, "active": is_active},
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
