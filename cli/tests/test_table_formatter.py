from __future__ import annotations

from inspire.cli.formatters.table import column_width, display_width, render_table


def test_column_width_counts_cjk_display_width() -> None:
    assert display_width("CPU资源空间") == 11
    assert column_width("Workspace", ["CPU资源空间"]) == 11


def test_render_table_scrubs_raw_ids_by_default() -> None:
    lines = render_table(
        ("Name", "Workspace"),
        [("train", "ws-12345678-1234-1234-1234-123456789abc")],
        [8, 14],
    )

    output = "\n".join(lines)
    assert "<redacted>" in output
    assert "ws-12345678-1234-1234-1234-123456789abc" not in output
