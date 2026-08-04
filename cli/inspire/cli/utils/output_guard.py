"""Final stdout/stderr firewall for platform handles."""

from __future__ import annotations

from typing import Any

import click

from inspire.cli.utils.raw_ids import scrub_raw_ids

_ORIGINAL_ECHO = click.echo
_ORIGINAL_SECHO = click.secho
_INSTALLED = False


def sanitize_output_message(message: Any) -> Any:
    """Scrub text immediately before it crosses the CLI output boundary."""
    if message is None:
        return None
    if isinstance(message, str):
        return scrub_raw_ids(message)
    if isinstance(message, bytes):
        try:
            return scrub_raw_ids(message.decode("utf-8")).encode("utf-8")
        except UnicodeDecodeError:
            return message
    return scrub_raw_ids(str(message))


def install_output_guard() -> None:
    """Make every subsequent ``click.echo`` / ``click.secho`` Name-only."""
    global _INSTALLED
    if _INSTALLED:
        return

    def _echo(message: Any = None, *args: Any, **kwargs: Any) -> None:
        _ORIGINAL_ECHO(sanitize_output_message(message), *args, **kwargs)

    def _secho(message: Any = None, *args: Any, **kwargs: Any) -> None:
        _ORIGINAL_SECHO(sanitize_output_message(message), *args, **kwargs)

    click.echo = _echo
    click.secho = _secho
    _INSTALLED = True


__all__ = ["install_output_guard", "sanitize_output_message"]
