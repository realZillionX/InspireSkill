"""Shared helpers for emitting CLI output in JSON and human modes."""

from __future__ import annotations

from typing import Any, Iterable

import click

from inspire.cli.context import Context
from inspire.cli.formatters import json_formatter


def _emit_debug_report_hint(ctx: Context) -> None:
    if not getattr(ctx, "debug", False):
        return
    debug_report_path = getattr(ctx, "debug_report_path", None)
    if not debug_report_path:
        return
    click.echo(f"Debug report: {debug_report_path}", err=True)


def emit_success(ctx: Context, *, payload: dict[str, Any], text: str | None = None) -> None:
    """Emit a success payload for JSON users or plain text for humans."""
    if ctx.json_output:
        click.echo(json_formatter.format_json(payload))
        return
    if text is not None:
        click.echo(text)


def emit_error(
    ctx: Context,
    *,
    error_type: str,
    message: str,
    exit_code: int,
    hint: str | None = None,
    human_lines: Iterable[str] | None = None,
) -> None:
    """Emit a formatted error in JSON mode, otherwise print provided human lines."""
    if ctx.json_output:
        click.echo(
            json_formatter.format_json_error(error_type, message, exit_code, hint=hint),
            err=True,
        )
        return

    if human_lines is not None:
        for line in human_lines:
            click.echo(line, err=True)
        _emit_debug_report_hint(ctx)
        return

    click.echo(message, err=True)
    if hint:
        click.echo(f"Hint: {hint}", err=True)
    _emit_debug_report_hint(ctx)
