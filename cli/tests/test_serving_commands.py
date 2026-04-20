"""Unit tests for `inspire.cli.commands.serving.serving_commands` rendering.

Focuses on the human-readable table renderer: empty state, full-page total,
and the "Showing X of Y" footer that replaces the misleading `len(rows)`-based
total when the caller is paginating. Complements the wire-format tests in
`test_browser_api_servings.py`.
"""

from __future__ import annotations

from inspire.cli.commands.serving.serving_commands import _format_list_rows


def _rows(n: int) -> list[dict[str, str]]:
    return [
        {
            "id": f"sv-{i:03d}",
            "name": f"demo-{i}",
            "status": "RUNNING",
            "replicas": "1",
            "created_at": "2026-04-20 10:00:00",
        }
        for i in range(n)
    ]


def test_format_list_rows_empty_message() -> None:
    assert _format_list_rows([], total=0) == "No inference servings found."


def test_format_list_rows_full_page_uses_total_line() -> None:
    out = _format_list_rows(_rows(3), total=3)
    # Header present, sep present, all 3 rows, Total: 3 footer.
    assert "Inference Servings" in out
    assert out.count("sv-") == 3
    assert "Total: 3" in out
    assert "Showing" not in out


def test_format_list_rows_paginated_uses_showing_line() -> None:
    # 5 visible rows but server reports 230 total → "Showing 5 of 230".
    out = _format_list_rows(_rows(5), total=230)
    assert "Showing 5 of 230" in out
    assert "Total:" not in out


def test_format_list_rows_total_matches_len_falls_back_to_total_line() -> None:
    """Edge: when total exactly matches len(rows), prefer the shorter Total line."""
    out = _format_list_rows(_rows(10), total=10)
    assert "Total: 10" in out
    assert "Showing" not in out
