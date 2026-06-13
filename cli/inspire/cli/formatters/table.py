"""Display-width aware table helpers for CLI human output."""

from __future__ import annotations

import unicodedata
from typing import Iterable, Literal, Sequence

Align = Literal["left", "right"]


def display_width(value: object) -> int:
    """Return terminal display width, counting CJK wide chars as two columns."""
    text = str(value)
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        if unicodedata.category(ch) in {"Cc", "Cf"}:
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
    suffix_width = display_width(suffix)
    limit = max(0, width - suffix_width)
    out: list[str] = []
    current = 0
    for ch in text:
        ch_width = display_width(ch)
        if current + ch_width > limit:
            break
        out.append(ch)
        current += ch_width
    return "".join(out) + suffix


def pad_cell(value: object, width: int, *, align: Align = "left") -> str:
    clipped = clip_display(value, width)
    padding = max(0, width - display_width(clipped))
    if align == "right":
        return (" " * padding) + clipped
    return clipped + (" " * padding)


def separator(widths: Sequence[int], *, char: str = "-") -> str:
    return char * (sum(widths) + max(0, len(widths) - 1))


def render_table(
    headers: Sequence[object],
    rows: Iterable[Sequence[object]],
    widths: Sequence[int],
    *,
    aligns: Sequence[str] | None = None,
    line_char: str = "-",
) -> list[str]:
    """Render a fixed-width table using display widths."""
    if aligns is None:
        aligns = ["left"] * len(widths)
    sep = separator(widths, char=line_char)
    lines = [
        sep,
        " ".join(
            pad_cell(header, width, align="right" if align == "right" else "left")
            for header, width, align in zip(headers, widths, aligns)
        ),
        sep,
    ]
    for row in rows:
        lines.append(
            " ".join(
                pad_cell(cell, width, align="right" if align == "right" else "left")
                for cell, width, align in zip(row, widths, aligns)
            )
        )
    lines.append(sep)
    return lines


__all__ = ["clip_display", "display_width", "pad_cell", "render_table", "separator"]
