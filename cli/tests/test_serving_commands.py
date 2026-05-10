"""Unit tests for `inspire.cli.commands.serving.serving_commands` rendering.

Focuses on the human-readable table renderer: empty state, full-page total,
and the "Showing X of Y" footer that replaces the misleading `len(rows)`-based
total when the caller is paginating. Complements the wire-format tests in
`test_browser_api_servings.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

from inspire.cli.commands.serving.serving_commands import (
    _build_resource_spec_price,
    _format_configs,
    _format_list_rows,
    _serving_image_label,
    _serving_model_label,
    _serving_resource_label,
)


def _rows(n: int) -> list[dict[str, str]]:
    return [
        {
            "id": f"sv-{i:03d}",
            "name": f"demo-{i}",
            "status": "RUNNING",
            "model": "qwen v1",
            "replicas": "1",
            "project": "demo-project",
            "updated_at": "2026-04-20 10:00:00",
        }
        for i in range(n)
    ]


def test_format_list_rows_empty_message() -> None:
    assert _format_list_rows([], total=0) == "No inference servings found."


def test_format_list_rows_full_page_uses_total_line() -> None:
    out = _format_list_rows(_rows(3), total=3)
    # Header present, sep present, all 3 rows, Total: 3 footer.
    assert "Inference Servings" in out
    # Platform handles are intentionally hidden in human format; row presence
    # is asserted via the names themselves.
    assert "sv-" not in out
    for i in range(3):
        assert f"demo-{i}" in out
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


def test_format_configs_renders_nested_config_shape() -> None:
    out = _format_configs(
        {
            "configs": {
                "enable_auto_stop": True,
                "items": [
                    {
                        "gpu_count_min": 8,
                        "gpu_count_max": 16,
                        "auto_stop_ruleset": (
                            '{"gate":"OR","conds":[{"crit":"GPU","thresh":20,"hrs":5}]}'
                        ),
                    }
                ],
            }
        }
    )

    assert "Auto-stop: enabled" in out
    assert "gpu=8-16" in out
    assert "GPU<20% for 5h" in out


def test_serving_status_helpers_render_nested_web_detail() -> None:
    detail = {
        "model": {"name": "demo-model", "version": 1, "id": "model-hidden"},
        "mirror": {"name": "sandbox-base", "version": "ubuntu24.04"},
        "resource_spec_price": {
            "cpu_count": 18,
            "memory_size_gib": 200,
            "gpu_count": 1,
            "gpu_info": {"gpu_type_display": "NVIDIA H200"},
        },
    }

    assert _serving_model_label(detail) == "demo-model v1"
    assert _serving_image_label(detail) == "sandbox-base:ubuntu24.04"
    assert _serving_resource_label(detail) == "18 CPU, 200 GiB, 1 GPU (NVIDIA H200)"


def test_build_resource_spec_price_uses_canonical_gpu_type() -> None:
    resolved = SimpleNamespace(
        cpu_count=18,
        gpu_count=1,
        memory_gib=200,
        logic_compute_group_id="lcg-1",
        quota_id="quota-1",
        raw_price={
            "cpu_info": {"cpu_type": "CPU_TYPE_INTEL"},
            "gpu_info": {
                "gpu_type": "NVIDIA_H200_SXM_141G",
                "gpu_type_display": "NVIDIA H200",
            },
        },
    )

    assert _build_resource_spec_price(resolved) == {
        "cpu_type": "CPU_TYPE_INTEL",
        "cpu_count": 18,
        "gpu_type": "NVIDIA_H200_SXM_141G",
        "gpu_count": 1,
        "memory_size_gib": 200,
        "logic_compute_group_id": "lcg-1",
        "quota_id": "quota-1",
    }
