"""Rich-backed table helpers for CLI human output.

The command modules intentionally keep their small, testable ``render_table``
adapter instead of writing to a global console.  The adapter now delegates the
actual layout to :class:`rich.table.Table`, so every human-facing table gets
the same width, alignment, truncation, and Unicode handling.
"""

from __future__ import annotations

import unicodedata
from io import StringIO
from typing import Iterable, Sequence

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from inspire.cli.utils.raw_ids import scrub_raw_ids

def _cell_text(value: object, *, scrub: bool = True) -> str:
    return scrub_raw_ids(value) if scrub else str(value)


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


def column_width(
    header: object,
    values: Iterable[object],
    *,
    max_width: int | None = None,
    scrub: bool = True,
) -> int:
    """Return a display-width aware column width for a header and values."""
    rendered = [_cell_text(value, scrub=scrub) for value in values]
    width = max(
        display_width(_cell_text(header, scrub=scrub)),
        *(display_width(v) for v in rendered),
        1,
    )
    if max_width is not None:
        return min(width, max_width)
    return width


def render_table(
    headers: Sequence[object],
    rows: Iterable[Sequence[object]],
    widths: Sequence[int],
    *,
    aligns: Sequence[str] | None = None,
    line_char: str = "-",
    scrub: bool = True,
    padding: tuple[int, int] = (0, 1),
) -> list[str]:
    """Render a table through Rich and return its plain-text lines.

    ``widths`` remains part of the adapter contract because callers use it to
    cap long, high-cardinality columns.  Rich performs the actual padding,
    alignment, Unicode width calculation, and ellipsis rendering.  Cells are
    passed as :class:`~rich.text.Text` objects so user data containing square
    brackets is never interpreted as Rich markup.

    ``line_char`` is retained as a source-compatible argument for command
    modules that used the old renderer.  Rich owns border glyphs now, so the
    value is intentionally ignored.

    ``padding`` follows Rich's ``(vertical, horizontal)`` convention and is
    exposed for the occasional compact table whose cells intentionally carry
    their own separator (for example, ``Workspace: permission``).
    """
    if aligns is None:
        aligns = ["left"] * len(widths)
    if len(headers) != len(widths):
        raise ValueError("table headers and widths must have the same length")
    if len(aligns) != len(widths):
        raise ValueError("table aligns and widths must have the same length")

    header_cells = [_cell_text(header, scrub=scrub) for header in headers]
    row_cells = [
        tuple(
            # Clip before handing values to Rich.  Rich's own ellipsis logic
            # may place a separate Unicode ellipsis token after a wide CJK
            # word, which makes a whitespace-delimited row ambiguous to
            # scripts consuming the compact human output.
            clip_display(_cell_text(cell, scrub=scrub), max(1, int(width)))
            for cell, width in zip(row, widths)
        )
        for row in rows
    ]
    for row in row_cells:
        if len(row) != len(headers):
            raise ValueError("table rows and headers must have the same length")

    table = Table(
        box=box.SIMPLE,
        show_header=True,
        show_footer=False,
        show_edge=False,
        expand=False,
        pad_edge=False,
        padding=padding,
        header_style="bold",
    )
    for header, width, align in zip(header_cells, widths, aligns):
        # Rich's column ``width`` includes the left and right padding.  The
        # callers calculate content widths, so reserve the two padding cells
        # here to avoid truncating a value that exactly fits its requested cap.
        rich_width = max(1, int(width)) + 2 * max(0, int(padding[1]))
        table.add_column(
            Text(header),
            justify="right" if align == "right" else "left",
            width=rich_width,
            max_width=rich_width,
            no_wrap=True,
            overflow="crop",
        )
    for row in row_cells:
        table.add_row(*(Text(cell) for cell in row))

    # Render to an isolated console so callers can continue to compose tables
    # with surrounding messages before handing the final text to Click.  The
    # width is deliberately large enough for the requested fixed columns;
    # otherwise Rich would wrap a wide resource table to its own 80-column
    # default even though the caller already supplied safe column caps.
    # Rich also reserves one separator cell between adjacent columns even for
    # ``box.SIMPLE``.  Include that cell in the off-screen console width so a
    # table whose values exactly fit their requested caps is not proportionally
    # shrunk by Rich's layout algorithm.
    table_width = sum(
        max(1, int(width)) + 2 * max(0, int(padding[1])) for width in widths
    ) + max(0, len(widths) - 1)
    stream = StringIO()
    console = Console(
        file=stream,
        force_terminal=False,
        color_system=None,
        width=max(80, table_width),
        soft_wrap=False,
    )
    console.print(table, end="")
    rendered = stream.getvalue().strip()
    return rendered.splitlines() if rendered else []


__all__ = [
    "clip_display",
    "column_width",
    "display_width",
    "render_table",
]
