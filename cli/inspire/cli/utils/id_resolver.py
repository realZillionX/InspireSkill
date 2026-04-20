"""Shared partial-UUID resolution utilities."""

from __future__ import annotations

import re

import click

from inspire.cli.context import Context, EXIT_VALIDATION_ERROR
from inspire.cli.utils.errors import exit_with_error


_FULL_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_HEX_RE = re.compile(r"^[0-9a-f]+$", re.IGNORECASE)

_MIN_PARTIAL_LEN = 4


def is_full_uuid(value: str, prefix: str | None = None) -> bool:
    """Return True if *value* is a full UUID, optionally with *prefix* stripped."""
    value = value.strip()
    if prefix and value.lower().startswith(prefix.lower()):
        value = value[len(prefix) :]
    return bool(_FULL_UUID_RE.match(value))


def is_partial_id(value: str, prefix: str | None = None) -> bool:
    """Return True if *value* looks like a partial hex ID (4+ hex chars, not a full UUID)."""
    value = value.strip()
    if prefix and value.lower().startswith(prefix.lower()):
        value = value[len(prefix) :]
    if len(value) < _MIN_PARTIAL_LEN:
        return False
    if is_full_uuid(value):
        return False
    return bool(_HEX_RE.match(value))


def normalize_partial(value: str, prefix: str | None = None) -> str:
    """Strip known *prefix* and return the lowercase hex portion."""
    value = value.strip()
    if prefix and value.lower().startswith(prefix.lower()):
        value = value[len(prefix) :]
    return value.lower()


def resolve_partial_id(
    ctx: Context,
    partial: str,
    resource_type: str,
    matches: list[tuple[str, str]],
    json_output: bool,
) -> str:
    """Disambiguate partial ID matches.

    *matches* is a list of ``(full_id, display_label)`` tuples.

    Returns the resolved full ID, or calls ``exit_with_error`` on failure.
    """
    if not matches:
        exit_with_error(
            ctx,
            "NotFound",
            f"No {resource_type} matching '{partial}'.",
            EXIT_VALIDATION_ERROR,
            hint=f"Run 'inspire {resource_type} list' to see available IDs.",
        )

    if len(matches) == 1:
        return matches[0][0]

    # Multiple matches
    if json_output:
        ids = [m[0] for m in matches]
        exit_with_error(
            ctx,
            "AmbiguousID",
            f"Partial ID '{partial}' matches {len(matches)} {resource_type}s: " + ", ".join(ids),
            EXIT_VALIDATION_ERROR,
            hint="Provide more characters to narrow the match.",
        )

    click.echo(f"Partial ID '{partial}' matches {len(matches)} {resource_type}s:")
    for idx, (full_id, label) in enumerate(matches, start=1):
        click.echo(f"  [{idx}] {full_id}  {label}")

    choice = click.prompt(
        f"Select {resource_type}",
        type=click.IntRange(1, len(matches)),
        default=1,
        show_default=True,
    )
    return matches[choice - 1][0]
