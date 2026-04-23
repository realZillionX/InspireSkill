"""``inspire account add <name>`` — create a new account directory."""

from __future__ import annotations

import click

from inspire.accounts import (
    AccountError,
    create_account,
    current_account,
    ensure_inspire_home,
    list_accounts,
    set_current_account,
    validate_name,
)

DEFAULT_BASE_URL = "https://qz.sii.edu.cn"
DEFAULT_PROXY_HINT = "http://127.0.0.1:7897"


@click.command("add")
@click.argument("name")
@click.option(
    "--username",
    help="Platform login username. Defaults to the account name; asked interactively if omitted.",
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
def add(
    name: str,
    username: str | None,
    password: str | None,
    base_url: str | None,
    proxy: str | None,
    make_active: bool | None,
    non_interactive: bool,
) -> None:
    """Create a new account at ``~/.inspire/accounts/<name>/``.

    By default walks you through five short prompts — platform username,
    password (with confirmation), base URL, proxy, and whether to switch
    to the new account. Any value passed via a flag skips the matching
    prompt. Pass ``--non-interactive`` to silence every prompt; missing
    fields fall back to defaults, and a missing ``--password`` aborts.

    \b
    Examples:
        # Interactive (recommended for first-time setup):
        inspire account add alice

        # Fully scripted (CI, automation):
        inspire account add alice \\
          --username user-abc123 --password "$INSPIRE_PW" \\
          --proxy http://127.0.0.1:7897 --use --non-interactive
    """
    try:
        validated = validate_name(name)
    except AccountError as err:
        raise click.ClickException(str(err)) from err

    if validated in list_accounts():
        raise click.ClickException(f"Account already exists: {validated}")

    ensure_inspire_home()

    # ---- username -------------------------------------------------------
    if username is None:
        if non_interactive:
            username = validated
        else:
            username = click.prompt(
                "Platform login username",
                default=validated,
                show_default=True,
            )
    resolved_username = username.strip()
    if not resolved_username:
        raise click.ClickException("Username cannot be empty.")

    # ---- password -------------------------------------------------------
    if password is None:
        if non_interactive:
            raise click.ClickException(
                "--password is required in non-interactive mode."
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
                f"Typical value: {DEFAULT_PROXY_HINT}"
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
        target = create_account(validated, content)
    except AccountError as err:
        raise click.ClickException(str(err)) from err

    click.echo(f"Created account: {target}")

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
        click.echo(f"Active account: {validated}")
    elif existing_active:
        click.echo(f"Active account unchanged: {existing_active}")


def _toml_basic(s: str) -> str:
    """Escape a string for a TOML basic (double-quoted) string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _render_config(*, username: str, password: str, base_url: str, proxy: str) -> str:
    lines = [
        f'username = "{_toml_basic(username)}"',
        f'password = "{_toml_basic(password)}"',
        f'base_url = "{_toml_basic(base_url)}"',
    ]
    if proxy:
        lines.append(f'proxy = "{_toml_basic(proxy)}"')
    return "\n".join(lines) + "\n"
