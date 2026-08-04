from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.commands.model import model_commands as model_commands_module
from inspire.cli.main import main as cli_main
from inspire.platform.web import browser_api as browser_api_module


_WORKSPACE_ID = "ws-11111111-1111-1111-1111-111111111111"
_FORBIDDEN_PUBLIC_KEYS = {
    "id",
    "model_id",
    "project_id",
    "workspace_id",
    "user_id",
    "raw",
    "payload",
    "result",
    "scanned",
    "source",
}


class _Session:
    workspace_id = _WORKSPACE_ID
    all_workspace_ids = [_WORKSPACE_ID]
    all_workspace_names = {_WORKSPACE_ID: "训练空间"}
    storage_state: dict[str, Any] = {}


def _json_data(output: str) -> Any:
    parsed = json.loads(output)
    return parsed.get("data", parsed)


def _assert_compact_public_payload(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            assert key not in _FORBIDDEN_PUBLIC_KEYS
            assert not key.endswith("_id")
            assert not key.endswith("_ids")
            _assert_compact_public_payload(child)
    elif isinstance(value, list):
        for child in value:
            _assert_compact_public_payload(child)


def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    projects: dict[str, str] | None = None,
    project_catalog: dict[str, dict[str, Any]] | None = None,
) -> config_module.Config:
    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
        log_cache_dir=str(tmp_path / "logs"),
        projects=projects or {},
        project_catalog=project_catalog or {},
    )
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, **kwargs: (config, {})),
    )
    monkeypatch.setattr(model_commands_module, "get_web_session", lambda: _Session())
    monkeypatch.setattr(
        browser_api_module,
        "get_current_user",
        lambda session=None: {"id": "user-secret-123"},
    )
    return config


def test_model_list_json_is_compact_and_name_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        browser_api_module,
        "list_models",
        lambda **kwargs: (
            [
                browser_api_module.ModelInfo(
                    model_id="model-secret-123",
                    id="42",
                    name="qwen-demo",
                    project_id="project-secret-123",
                    project_name="模型项目",
                    workspace_id=_WORKSPACE_ID,
                    user_id="user-secret-123",
                    user_name="Alice",
                    status="2",
                    updated_at="2026-08-01T12:00:00Z",
                    latest_version="3",
                    model_path="/inspire/ssd/project/topic/model-secret-123",
                    model_source_path="/internal/source",
                    raw={"payload": {"trace": "secret"}},
                )
            ],
            900,
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "model", "list", "--workspace", "训练空间"],
    )

    assert result.exit_code == 0, result.output
    payload = _json_data(result.output)
    assert payload == {
        "models": [
            {
                "name": "qwen-demo",
                "version": "V3",
                "status": "SUCCESS",
                "project": "模型项目",
                "updated_at": "2026-08-01T12:00:00Z",
            }
        ]
    }
    assert "total" not in payload
    _assert_compact_public_payload(payload)
    for secret in (
        "model-secret-123",
        "project-secret-123",
        _WORKSPACE_ID,
        "user-secret-123",
        "/internal/source",
    ):
        assert secret not in result.output


def test_model_list_human_has_no_registry_banner_or_totals(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        browser_api_module,
        "list_models",
        lambda **kwargs: (
            [
                browser_api_module.ModelInfo(
                    model_id="model-secret-123",
                    name="qwen-demo",
                    project_name="模型项目",
                    status="2",
                    latest_version="v3",
                )
            ],
            900,
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        ["model", "list", "--workspace", "训练空间"],
    )

    assert result.exit_code == 0, result.output
    assert "qwen-demo" in result.output
    assert "V3" in result.output
    assert "Model Registry" not in result.output
    assert "Total:" not in result.output
    assert "Showing " not in result.output
    assert "Owner" not in result.output
    assert "model-secret-123" not in result.output


def test_model_status_json_removes_paths_ids_and_raw_records(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        model_commands_module,
        "_resolve_model_name",
        lambda *args, **kwargs: "model-secret-456",
    )
    monkeypatch.setattr(
        browser_api_module,
        "get_model_detail",
        lambda **kwargs: {
            "model": {
                "model_id": "model-secret-456",
                "name": "qwen-demo",
                "status": 1,
                "description": "Chat model",
                "model_type": ["NLP", "TextGeneration"],
                "tags": ["chat"],
                "has_published": True,
                "model_path": "/inspire/ssd/models/model-secret-456",
                "model_source_path": "/internal/source",
            },
            "project_id": "project-secret-456",
            "project_name": "模型项目",
            "user_id": "user-secret-456",
            "user_name": "Alice",
            "payload": {"trace": "secret"},
        },
    )
    monkeypatch.setattr(
        browser_api_module,
        "list_model_version_records",
        lambda **kwargs: {
            "list": [
                {
                    "model": {
                        "model_id": "model-secret-456",
                        "version": 4,
                        "status": 2,
                        "is_vllm_compatible": True,
                        "model_path": "/inspire/ssd/models/model-secret-456/v4",
                        "model_source_path": "/internal/source/v4",
                    },
                    "raw": {"request_id": "trace-secret"},
                }
            ],
            "total": 1,
            "next_version": 5,
        },
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "model", "status", "qwen-demo", "--workspace", "训练空间"],
    )

    assert result.exit_code == 0, result.output
    payload = _json_data(result.output)
    assert payload == {
        "name": "qwen-demo",
        "status": "SUCCESS",
        "version": "V4",
        "description": "Chat model",
        "type": ["NLP", "TextGeneration"],
        "tags": ["chat"],
        "vllm_ready": True,
        "published": True,
        "project": "模型项目",
        "owner": "Alice",
    }
    _assert_compact_public_payload(payload)
    assert "path" not in payload
    assert "total" not in payload
    for secret in (
        "model-secret-456",
        "project-secret-456",
        "user-secret-456",
        "/internal/source",
        "trace-secret",
    ):
        assert secret not in result.output


def test_model_versions_json_only_keeps_actionable_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        model_commands_module,
        "_resolve_model_name",
        lambda *args, **kwargs: "model-secret-789",
    )
    monkeypatch.setattr(
        browser_api_module,
        "list_model_version_records",
        lambda **kwargs: {
            "list": [
                {
                    "model": {
                        "model_id": "model-secret-789",
                        "version": 1,
                        "status": 2,
                        "model_size_gi": 12.5,
                        "is_vllm_compatible": True,
                        "model_path": "/internal/model",
                        "model_source_path": "/internal/source",
                    },
                    "running_infrence_serving": 2,
                },
                {
                    "model": {
                        "version": "v2",
                        "status": 3,
                        "model_size_gi": 2048,
                    }
                },
            ],
            "total": 2,
            "next_version": 3,
            "payload": {"trace": "secret"},
        },
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "model", "versions", "qwen-demo", "--workspace", "训练空间"],
    )

    assert result.exit_code == 0, result.output
    payload = _json_data(result.output)
    assert payload == {
        "name": "qwen-demo",
        "versions": [
            {
                "version": "V1",
                "status": "SUCCESS",
                "size": "12.50 GiB",
                "vllm_ready": True,
                "running_servings": 2,
            },
            {
                "version": "V2",
                "status": "FAILED",
                "size": "2.00 TiB",
                "vllm_ready": False,
            },
        ],
    }
    _assert_compact_public_payload(payload)
    assert "total" not in payload
    assert "next_version" not in payload
    assert "/internal/model" not in result.output
    assert "/internal/source" not in result.output
    assert "model-secret-789" not in result.output


def test_model_register_resolves_alias_to_current_live_project_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_runtime(
        monkeypatch,
        tmp_path,
        projects={"production": "模型项目"},
        project_catalog={"production": {"name": "模型项目"}},
    )
    monkeypatch.setattr(
        browser_api_module,
        "list_projects",
        lambda **kwargs: [
            browser_api_module.ProjectInfo(
                project_id="project-current-123",
                name="模型项目",
                workspace_id=_WORKSPACE_ID,
            )
        ],
    )
    captured: dict[str, Any] = {}

    def _create_model(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return {
            "model_id": "model-secret-created",
            "payload": {"project_id": kwargs["project_id"]},
        }

    monkeypatch.setattr(browser_api_module, "create_model", _create_model)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "model",
            "register",
            "--name",
            "qwen-demo",
            "--source-path",
            "/inspire/ssd/project/topic/public/qwen-demo",
            "--workspace",
            "训练空间",
            "--project",
            "production",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["workspace_id"] == _WORKSPACE_ID
    assert captured["project_id"] == "project-current-123"
    payload = _json_data(result.output)
    assert payload == {
        "name": "qwen-demo",
        "status": "registered",
        "project": "production",
        "workspace": "训练空间",
    }
    _assert_compact_public_payload(payload)
    assert "project-current-123" not in result.output
    assert "model-secret-created" not in result.output


def test_legacy_project_alias_uses_live_name_not_stale_id() -> None:
    config = config_module.Config(
        username="user",
        password="pass",
        projects={"production": "project-old-123"},
        project_catalog={"project-old-123": {"name": "模型项目"}},
    )
    current = browser_api_module.ProjectInfo(
        project_id="project-new-456",
        name="模型项目",
        workspace_id=_WORKSPACE_ID,
    )

    original = browser_api_module.list_projects
    try:
        browser_api_module.list_projects = lambda **kwargs: [current]  # type: ignore[assignment]
        resolved = model_commands_module._resolve_project_id(
            config,
            "production",
            workspace_id=_WORKSPACE_ID,
            session=_Session(),
        )
    finally:
        browser_api_module.list_projects = original  # type: ignore[assignment]

    assert resolved == "project-new-456"


def test_legacy_project_alias_without_name_never_falls_back_to_stale_id() -> None:
    config = config_module.Config(
        username="user",
        password="pass",
        projects={"production": "project-old-123"},
    )
    renamed_old_project = browser_api_module.ProjectInfo(
        project_id="project-old-123",
        name="已重命名项目",
        workspace_id=_WORKSPACE_ID,
    )

    original = browser_api_module.list_projects
    try:
        browser_api_module.list_projects = lambda **kwargs: [renamed_old_project]  # type: ignore[assignment]
        with pytest.raises(config_module.ConfigError, match="Unknown project name"):
            model_commands_module._resolve_project_id(
                config,
                "production",
                workspace_id=_WORKSPACE_ID,
                session=_Session(),
            )
    finally:
        browser_api_module.list_projects = original  # type: ignore[assignment]
