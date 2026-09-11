"""Text projections shared by structured resource views and CLI formatting."""

from __future__ import annotations

from datetime import datetime
from typing import Any
import unicodedata


def format_epoch(value: Any) -> str:
    """Format an epoch in seconds or milliseconds for display."""
    if value is None or value == "":
        return "-"
    try:
        epoch = int(str(value))
    except (ValueError, TypeError):
        return str(value)
    if epoch <= 0:
        return "-"
    if epoch >= 100_000_000_000:
        epoch //= 1000
    try:
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "-"


def display_width(value: object) -> int:
    """Return terminal display width, counting CJK wide chars as two columns."""
    width = 0
    for ch in str(value):
        if unicodedata.combining(ch) or unicodedata.category(ch) in {"Cc", "Cf"}:
            continue
        width += 2 if unicodedata.east_asian_width(ch) in {"F", "W"} else 1
    return width


def clip_display(value: object, width: int) -> str:
    """Clip text to a display width without splitting wide characters."""
    text = str(value)
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    suffix = "..." if width >= 4 else "." * width
    limit = width - display_width(suffix)
    out: list[str] = []
    current = 0
    for ch in text:
        ch_width = display_width(ch)
        if current + ch_width > limit:
            break
        out.append(ch)
        current += ch_width
    return "".join(out) + suffix
