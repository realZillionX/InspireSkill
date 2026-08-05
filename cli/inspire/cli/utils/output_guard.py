"""Redaction for Click's own parser diagnostics.

Command output is sanitized by the formatters that build it — they know
which field is a name, which is a log line, and which is a message we wrote
ourselves. Click's parser errors are the one surface with no formatter in
front of them, so they get their own narrow pass here.
"""

from __future__ import annotations

import re
from typing import Any

import click

from inspire.cli.utils.raw_ids import scrub_raw_ids

_PARSER_REDACTIONS: tuple[re.Pattern[str], ...] = ()


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


def set_parser_redactions(args: list[str] | tuple[str, ...]) -> None:
    """Remember sensitive argv tokens only for Click parser diagnostics."""
    from inspire.cli.utils.id_resolver import is_partial_id, looks_like_platform_id

    values: set[str] = set()

    def _looks_like_path(value: str) -> bool:
        return bool(
            value.startswith(("/", "~/", "./", "../"))
            or re.match(r"^[A-Za-z]:[\\/]", value)
        )

    for raw_arg in args:
        token = str(raw_arg or "")
        candidates = [token]
        if "=" in token:
            candidates.append(token.split("=", maxsplit=1)[1])
        for candidate in candidates:
            value = candidate.strip()
            # Parser diagnostics are not a name-resolution surface. Redact
            # handle-shaped values there while preserving legitimate names.
            if value and (
                looks_like_platform_id(value)
                or is_partial_id(value)
                or _looks_like_path(value)
            ):
                values.add(value)

    global _PARSER_REDACTIONS
    _PARSER_REDACTIONS = tuple(
        re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        for value in sorted(values, key=len, reverse=True)
    )


def clear_parser_redactions() -> None:
    """Clear transient argv redactions after one CLI invocation."""
    global _PARSER_REDACTIONS
    _PARSER_REDACTIONS = ()


def sanitize_parser_message(message: Any) -> Any:
    """Sanitize Click parser output without touching normal command content."""
    sanitized = sanitize_output_message(message)
    if not isinstance(sanitized, (str, bytes)):
        return sanitized
    if isinstance(sanitized, bytes):
        try:
            text = sanitized.decode("utf-8")
        except UnicodeDecodeError:
            return sanitized
        return_value_as_bytes = True
    else:
        text = sanitized
        return_value_as_bytes = False
    for pattern in _PARSER_REDACTIONS:
        text = pattern.sub("<redacted>", text)
    return text.encode("utf-8") if return_value_as_bytes else text


def parser_echo(message: Any = None, *args: Any, **kwargs: Any) -> None:
    """Echo Click parser diagnostics after applying argv redactions."""
    click.echo(sanitize_parser_message(message), *args, **kwargs)


__all__ = [
    "clear_parser_redactions",
    "parser_echo",
    "sanitize_output_message",
    "sanitize_parser_message",
    "set_parser_redactions",
]
