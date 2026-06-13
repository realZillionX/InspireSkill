"""Compatibility wrapper for display-width aware table helpers."""

from __future__ import annotations

from inspire.cli.formatters.table import (
    clip_display,
    display_width,
    pad_cell,
    render_table,
    separator,
)


__all__ = ["clip_display", "display_width", "pad_cell", "render_table", "separator"]
