"""Shared output budgets for collection-style CLI commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Sequence, TypeVar


DEFAULT_COLLECTION_LIMIT = 20

T = TypeVar("T")


@dataclass(frozen=True)
class BoundedCollection(Generic[T]):
    """A collection clipped to the public CLI output budget."""

    items: list[T]
    shown: int
    total: int
    truncated: bool

    def metadata(self, *, always: bool = False) -> dict[str, int | bool]:
        """Return JSON metadata when truncated, or always for existing schemas."""
        if not self.truncated and not always:
            return {}
        return {
            "shown": self.shown,
            "total": self.total,
            "truncated": self.truncated,
        }


def resolve_collection_limit(
    *,
    limit: int | None,
    show_all: bool,
    default: int = DEFAULT_COLLECTION_LIMIT,
) -> int | None:
    """Resolve ``--limit``/``--all`` without allowing ambiguous combinations."""
    if show_all and limit is not None:
        raise ValueError("Use either --limit or --all, not both.")
    if show_all:
        return None
    return limit if limit is not None else default


def bound_collection(
    items: Sequence[T],
    *,
    limit: int | None,
    total: int | None = None,
) -> BoundedCollection[T]:
    """Clip items and report whether the caller omitted matching rows."""
    materialized = list(items)
    try:
        reported_total = int(total) if total is not None else 0
    except (TypeError, ValueError):
        reported_total = 0
    known_total = max(len(materialized), reported_total)
    bounded = materialized if limit is None else materialized[:limit]
    return BoundedCollection(
        items=bounded,
        shown=len(bounded),
        total=known_total,
        truncated=known_total > len(bounded),
    )


def truncation_notice(
    collection: BoundedCollection[T],
    *,
    full_option: str = "--all",
) -> str | None:
    """Return a short human hint only when output was actually clipped."""
    if not collection.truncated:
        return None
    return (
        f"Showing {collection.shown} of {collection.total}. "
        f"Use {full_option} for the full list."
    )


__all__ = [
    "DEFAULT_COLLECTION_LIMIT",
    "BoundedCollection",
    "bound_collection",
    "resolve_collection_limit",
    "truncation_notice",
]
