"""Shared partial-UUID resolution utilities."""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Optional

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


# ---------------------------------------------------------------------------
# name → id resolver (for job / hpc / ray / serving / image, etc.)
# ---------------------------------------------------------------------------


def resolve_by_name(
    ctx: Context,
    *,
    name: str,
    resource_type: str,
    list_candidates: Callable[[], Iterable[dict[str, Any]]],
    json_output: bool = False,
    name_key: str = "name",
    id_key: str = "id",
    label_fn: Optional[Callable[[dict[str, Any]], str]] = None,
) -> str:
    """Resolve a platform name to its internal id.

    The v2 CLI contract: **only names** cross the user / agent boundary.
    Platform-internal ids (``job-…`` / ``hpc-job-…`` / ``rj-…`` / ``sv-…``
    / ``image-…`` / raw UUIDs) are rejected to keep downstream prompts
    unambiguous.

    ``list_candidates()`` returns dicts with at least ``name_key`` and
    ``id_key``. Exact string match on ``name_key``; multiple matches abort
    with the full candidate list (we never silently send an action to the
    wrong target — two jobs with the same name would otherwise have you
    stop the wrong one).
    """
    name = (name or "").strip()
    if not name:
        exit_with_error(
            ctx,
            "ValidationError",
            f"{resource_type} name cannot be empty",
            EXIT_VALIDATION_ERROR,
        )

    # Reject id-looking inputs — v2.0.0 removed id compatibility.
    if _looks_like_platform_id(name):
        exit_with_error(
            ctx,
            "ValidationError",
            f"v2 CLI takes a {resource_type} name, not an id ({name!r}).",
            EXIT_VALIDATION_ERROR,
            hint=(
                f"Find the name with `inspire {resource_type} list` and pass that. "
                "Ids are intentionally not accepted on the v2 CLI — stop surfacing "
                "them to agents."
            ),
        )
        return ""  # unreachable

    try:
        candidates = list(list_candidates())
    except Exception as e:  # noqa: BLE001
        exit_with_error(
            ctx,
            "APIError",
            f"Failed to resolve {resource_type} name {name!r}: {e}",
            EXIT_VALIDATION_ERROR,
        )
        return ""  # unreachable

    matches = [c for c in candidates if str(c.get(name_key) or "") == name]

    if not matches:
        exit_with_error(
            ctx,
            "NotFound",
            f"No {resource_type} with name {name!r} found.",
            EXIT_VALIDATION_ERROR,
            hint=f"List candidates with `inspire {resource_type} list` (or `-A`).",
        )
        return ""  # unreachable

    if len(matches) == 1:
        return str(matches[0].get(id_key) or "")

    def _label(c: dict[str, Any]) -> str:
        if label_fn is not None:
            return label_fn(c)
        bits = []
        status = c.get("status")
        if status:
            bits.append(str(status))
        created = c.get("created_at")
        if created:
            bits.append(f"created_at={created}")
        ws = c.get("workspace_id")
        if ws:
            bits.append(f"ws={ws}")
        return "  ".join(bits) if bits else ""

    lines = [f"  [{i}] {_label(c)}" for i, c in enumerate(matches, start=1)]
    exit_with_error(
        ctx,
        "AmbiguousName",
        f"{len(matches)} {resource_type}s share the name {name!r}:\n" + "\n".join(lines),
        EXIT_VALIDATION_ERROR,
        hint=(
            "Narrow the candidate set by workspace or status on your `list` call, "
            "or rename one of the duplicates."
        ),
    )
    return ""  # unreachable


def _looks_like_platform_id(value: str) -> bool:
    """Heuristic for id-shaped inputs we reject in the v2 CLI.

    Catches the common prefixes (``job-`` / ``hpc-job-`` / ``rj-`` / ``sv-``
    / ``image-`` / ``notebook-`` / ``nb-``) and bare full UUIDs.
    """
    v = value.strip().lower()
    if not v:
        return False
    id_prefixes = ("job-", "hpc-job-", "rj-", "sv-", "image-", "notebook-", "nb-")
    if any(v.startswith(p) for p in id_prefixes):
        return True
    # Bare UUID — stripping only colons/underscores would be wrong, just match exactly.
    return bool(_FULL_UUID_RE.match(v))
