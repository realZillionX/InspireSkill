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


@click.command("add")
@click.argument("name")
@click.option(
    "--username",
    help="Inspire login username on the platform. Defaults to the account name.",
)
@click.option(
    "--password",
    help="Inspire password. Prompted securely if omitted.",
)
@click.option(
    "--base-url",
    default=DEFAULT_BASE_URL,
    show_default=True,
    help="Inspire platform base URL.",
)
@click.option(
    "--proxy",
    help="HTTP/SOCKS5 proxy reachable to both the public internet and *.sii.edu.cn.",
)
@click.option(
    "--use/--no-use",
    "make_active",
    default=None,
    help="Set as active after creation. Defaults to true if no account is active yet.",
)
def add(
    name: str,
    username: str | None,
    password: str | None,
    base_url: str,
    proxy: str | None,
    make_active: bool | None,
) -> None:
    """Create a new account at ``~/.inspire/accounts/<name>/``.

    Writes a minimal ``config.toml`` containing username / password / base_url
    / proxy. Everything else (workspaces, projects, compute groups, paths,
    default images) can be filled in later by editing that file directly or
    running ``inspire init`` under the active account.

    \b
    Examples:
        inspire account add alice
        inspire account add alice --proxy http://127.0.0.1:7897 --use
        inspire account add bob --username user-abc123
    """
    try:
        validated = validate_name(name)
    except AccountError as err:
        raise click.ClickException(str(err)) from err

    if validated in list_accounts():
        raise click.ClickException(f"Account already exists: {validated}")

    ensure_inspire_home()

    resolved_username = (username or validated).strip()
    if not resolved_username:
        raise click.ClickException("Username cannot be empty.")

    if password is None:
        password = click.prompt("Password", hide_input=True, confirmation_prompt=False)

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

    should_activate = make_active
    if should_activate is None:
        should_activate = current_account() is None
    if should_activate:
        set_current_account(validated)
        click.echo(f"Active account: {validated}")


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
