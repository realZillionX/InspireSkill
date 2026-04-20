"""Shared CLI error handling utilities.

Centralizes JSON vs human formatting and consistent exit codes across commands.
"""

from __future__ import annotations

import sys

import click

from inspire.cli.context import EXIT_GENERAL_ERROR, Context
from inspire.cli.formatters import human_formatter, json_formatter


def _emit_debug_report_hint(ctx: Context) -> None:
    if not getattr(ctx, "debug", False):
        return
    debug_report_path = getattr(ctx, "debug_report_path", None)
    if not debug_report_path:
        return
    click.echo(f"Debug report: {debug_report_path}", err=True)


def emit_error(
    ctx: Context,
    error_type: str,
    message: str,
    exit_code: int = EXIT_GENERAL_ERROR,
    *,
    hint: str | None = None,
) -> int:
    """Emit a formatted error without exiting. Returns exit_code."""
    if ctx.json_output:
        click.echo(
            json_formatter.format_json_error(error_type, message, exit_code, hint=hint),
            err=True,
        )
    else:
        click.echo(human_formatter.format_error(message, hint=hint), err=True)
        _emit_debug_report_hint(ctx)
    return exit_code


def exit_with_error(
    ctx: Context,
    error_type: str,
    message: str,
    exit_code: int = EXIT_GENERAL_ERROR,
    *,
    hint: str | None = None,
) -> None:
    """Print a formatted error and exit with the given code."""
    emit_error(ctx, error_type, message, exit_code, hint=hint)
    sys.exit(exit_code)
