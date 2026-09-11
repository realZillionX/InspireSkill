"""Render byte counts the way every command that shows one already does."""
from __future__ import annotations


_SIZE_UNITS: tuple[tuple[str, int], ...] = (
    ("TiB", 1024**4),
    ("GiB", 1024**3),
    ("MiB", 1024**2),
    ("KiB", 1024),
)


def format_size_bytes(value: int) -> str:
    for label, divisor in _SIZE_UNITS:
        if value >= divisor:
            return f"{value / divisor:.2f} {label}"
    return f"{value} B"
