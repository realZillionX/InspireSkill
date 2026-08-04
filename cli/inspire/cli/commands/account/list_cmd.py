"""``inspire account list`` — list all accounts, marking the active one."""

from __future__ import annotations

import click

from inspire.accounts import current_account, list_accounts
from inspire.cli.context import Context, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.output import emit_success


@click.command("list")
@pass_context
def list_cmd(ctx: Context) -> None:
    """List all configured accounts. Active account is marked with ``*``."""
    names = list_accounts()
    active = current_account()
    payload = {
        "accounts": [{"name": name, "active": name == active} for name in names]
    }
    if not names:
        emit_success(
            ctx,
            payload=payload,
            text="No accounts configured.",
        )
        return
    lines = [
        f" {'*' if name == active else ' '} {name}"
        for name in names
    ]
    emit_success(
        ctx,
        payload=payload,
        text=json_formatter.sanitize_text("\n".join(lines), redact_paths=True),
    )
