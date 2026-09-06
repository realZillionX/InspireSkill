"""``inspire account list`` — list all accounts, marking the active one."""

from __future__ import annotations

import click

from inspire.accounts import default_account, list_accounts
from inspire.cli.context import Context, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.collection_output import (
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)
from inspire.cli.utils.output import emit_success


@click.command("list")
@click.option(
    "--limit",
    "-n",
    type=click.IntRange(1),
    default=None,
    help="Maximum accounts to display (default: 20).",
)
@click.option("--all", "show_all", is_flag=True, help="Show every configured account.")
@pass_context
def list_cmd(ctx: Context, limit: int | None, show_all: bool) -> None:
    """List all configured accounts. Active account is marked with ``*``."""
    try:
        output_limit = resolve_collection_limit(limit=limit, show_all=show_all)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    names = list_accounts()
    active = default_account()
    page = bound_collection(
        [{"name": name, "active": name == active} for name in names],
        limit=output_limit,
    )
    payload = {"items": page.items, **page.metadata()}
    if not names:
        emit_success(
            ctx,
            payload=payload,
            text="No accounts configured.",
        )
        return
    lines = [
        f" {'*' if item['active'] else ' '} {item['name']}"
        for item in page.items
    ]
    notice = truncation_notice(page)
    if notice:
        lines.append(notice)
    emit_success(
        ctx,
        payload=payload,
        text=json_formatter.sanitize_text("\n".join(lines), redact_paths=True),
    )
