"""Unit tests for `inspire.cli.commands.serving.serving_commands` rendering.

Focuses on the human-readable table renderer: empty state, full-page total,
and the "Showing X of Y" footer that replaces the misleading `len(rows)`-based
total when the caller is paginating. Complements the wire-format tests in
`test_browser_api_servings.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.commands.serving import serving_commands as serving_commands_module
from inspire.cli.commands.serving.serving_commands import (
    _build_resource_spec_price,
    _format_configs,
    _format_list_rows,
    _serving_image_label,
    _serving_model_label,
    _serving_resource_label,
)
from inspire.cli.main import main as cli_main
from inspire.platform.web import browser_api as browser_api_module

_REAL_RESOLVE_SERVING_NAME = serving_commands_module._resolve_serving_name


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


def test_format_list_rows_is_compact_and_handle_free() -> None:
    out = _format_list_rows(_rows(3), total=3)
    assert "Inference Servings" not in out
    assert "Total:" not in out
    assert "Showing" not in out
    assert "---" not in out
    assert "sv-" not in out
    for i in range(3):
        assert f"demo-{i}" in out


def test_format_list_rows_does_not_emit_pagination_footer() -> None:
    out = _format_list_rows(_rows(5), total=230)
    assert "demo-0" in out
    assert "Total:" not in out
    assert "Showing" not in out


def test_format_list_rows_ignores_server_total() -> None:
    out = _format_list_rows(_rows(10), total=10)
    assert "demo-9" in out
    assert "Inference Servings" not in out
    assert "Showing" not in out
    assert "Total:" not in out


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

    assert "auto-stop=enabled" in out
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


class FakeSession:
    storage_state: dict[str, Any] = {}


def _patch_delete_deps(monkeypatch) -> dict[str, Any]:  # noqa: ANN001
    calls: dict[str, Any] = {}
    config = config_module.Config(username="user", password="pass")

    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(serving_commands_module, "get_web_session", lambda: FakeSession())
    monkeypatch.setattr(
        serving_commands_module,
        "_resolve_workspace_id",
        lambda config, workspace: "ws-1",
    )
    monkeypatch.setattr(
        serving_commands_module,
        "_resolve_serving_name",
        lambda ctx, name, workspace_id=None, pick=None: "sv-1",
    )

    def fake_delete_serving(*, inference_serving_id: str, session=None) -> dict[str, Any]:
        calls["serving_id"] = inference_serving_id
        return {"ok": True}

    monkeypatch.setattr(browser_api_module, "delete_serving", fake_delete_serving)
    return calls


def test_serving_raw_handle_is_rejected_before_detail_api(monkeypatch) -> None:  # noqa: ANN001
    config = config_module.Config(username="user", password="pass")
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(serving_commands_module, "get_web_session", lambda: FakeSession())
    monkeypatch.setattr(
        serving_commands_module,
        "_resolve_workspace_id",
        lambda _config, _workspace: "ws-1",
    )
    monkeypatch.setattr(
        serving_commands_module,
        "_resolve_serving_name",
        _REAL_RESOLVE_SERVING_NAME,
    )
    monkeypatch.setattr(
        browser_api_module,
        "get_serving_detail",
        lambda **_kwargs: pytest.fail("raw handle must be rejected before API lookup"),
    )

    result = CliRunner().invoke(
        cli_main,
        ["serving", "status", "sv-12345678", "--workspace", "Test Workspace"],
    )

    assert result.exit_code != 0
    assert "platform handle" in result.output


def test_serving_delete_prompts_by_default(monkeypatch) -> None:  # noqa: ANN001
    calls = _patch_delete_deps(monkeypatch)

    result = CliRunner().invoke(
        cli_main,
        ["serving", "delete", "demo", "--workspace", "Test Workspace"],
        input="n\n",
    )

    assert result.exit_code == 1
    assert calls == {}


def test_serving_delete_yes_skips_prompt(monkeypatch) -> None:  # noqa: ANN001
    calls = _patch_delete_deps(monkeypatch)

    result = CliRunner().invoke(
        cli_main,
        ["serving", "delete", "demo", "--workspace", "Test Workspace", "--yes"],
    )

    assert result.exit_code == 0
    assert calls["serving_id"] == "sv-1"
    assert "Inference serving deleted: demo" in result.output


def test_serving_create_rejects_invalid_custom_domain() -> None:
    result = CliRunner().invoke(
        cli_main,
        [
            "serving",
            "create",
            "--name",
            "demo",
            "--model",
            "model-a",
            "--workspace",
            "Test Workspace",
            "--project",
            "Project",
            "--group",
            "H200 Room",
            "--quota",
            "1,18,200",
            "--image",
            "serve:v1",
            "--command",
            "python serve.py",
            "--port",
            "8000",
            "--custom-domain",
            "Bad_Domain",
            "--dry-run",
        ],
    )

    assert result.exit_code != 0
    assert "Invalid value for '--custom-domain'" in result.output
