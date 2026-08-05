"""Shared helpers for emitting CLI output in JSON and human modes."""

from __future__ import annotations

from typing import Any

import click

from inspire.cli.context import Context
from inspire.cli.formatters import json_formatter


def emit_success(ctx: Context, *, payload: dict[str, Any], text: str | None = None) -> None:
    """Emit a success payload for JSON users or plain text for humans."""
    if ctx.json_output:
        click.echo(json_formatter.format_json(payload))
        return
    if text is not None:
        click.echo(text)
