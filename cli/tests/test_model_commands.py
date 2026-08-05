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
_SECOND_WORKSPACE_ID = "ws-22222222-2222-2222-2222-222222222222"
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
                    user_name="253108120116",
                    status="2",
                    updated_at="2026-08-01T12:00:00Z",
                    latest_version="3",
                    model_source_path="/internal/source",
                    raw={
                        "payload": {"trace": "secret"},
                        "model_path": "/inspire/ssd/project/topic/model-secret-123",
                        "user_name": "253108120116",
                        "username": "usr_391",
                        "login_name": "student-42",
                        "created_by_name": "Alice",
                    },
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
        "items": [
            {
                "name": "qwen-demo",
                "status": "SUCCESS",
                "project": "模型项目",
                "workspace": "训练空间",
                "created_by": "Alice",
                "version": "V3",
                "updated_at": "2026-08-01T12:00:00Z",
            }
        ],
        "shown": 1,
        "total": 900,
        "truncated": True,
    }
    _assert_compact_public_payload(payload)
    for secret in (
        "model-secret-123",
        "project-secret-123",
        _WORKSPACE_ID,
        "user-secret-123",
        "/internal/source",
    ):
        assert secret not in result.output


def test_model_list_human_has_compact_truncation_hint(
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
    assert "Showing 1 of 900" in result.output
    assert "Use --all" in result.output
    assert "Owner" not in result.output
    assert "model-secret-123" not in result.output


def test_model_list_all_expands_and_limit_conflict_is_pre_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    calls: list[int] = []

    def fake_list_models(**kwargs):  # noqa: ANN001
        calls.append(kwargs["page_size"])
        count = kwargs["page_size"]
        return (
            [
                browser_api_module.ModelInfo(
                    model_id=f"model-{index}",
                    name=f"model-{index}",
                    status="2",
                )
                for index in range(count)
            ],
            25,
        )

    monkeypatch.setattr(browser_api_module, "list_models", fake_list_models)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "model", "list", "--workspace", "训练空间", "--all"],
    )

    assert result.exit_code == 0, result.output
    payload = _json_data(result.output)
    assert calls == [20, 25]
    assert len(payload["items"]) == 25
    assert "models" not in payload
    assert "truncated" not in payload

    calls.clear()
    conflict = CliRunner().invoke(
        cli_main,
        [
            "model",
            "list",
            "--workspace",
            "训练空间",
            "--all",
            "--limit",
            "3",
        ],
    )
    assert conflict.exit_code != 0
    assert "Use either --limit or --all, not both." in conflict.output
    assert calls == []


def test_model_list_workspace_all_fans_out_and_labels_each_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_runtime(monkeypatch, tmp_path)

    class _AllWorkspaceSession:
        workspace_id = _WORKSPACE_ID
        all_workspace_ids = [_WORKSPACE_ID, _SECOND_WORKSPACE_ID]
        all_workspace_names = {
            _WORKSPACE_ID: "训练空间",
            _SECOND_WORKSPACE_ID: "推理空间",
        }
        storage_state: dict[str, Any] = {}

    calls: list[str] = []
    monkeypatch.setattr(
        model_commands_module,
        "get_web_session",
        lambda: _AllWorkspaceSession(),
    )

    def fake_list_models(**kwargs):  # noqa: ANN001
        workspace_id = kwargs["workspace_id"]
        calls.append(workspace_id)
        suffix = "train" if workspace_id == _WORKSPACE_ID else "serve"
        return (
            [
                browser_api_module.ModelInfo(
                    model_id=f"model-secret-{suffix}",
                    name=f"model-{suffix}",
                    project_name="模型项目",
                    workspace_id=workspace_id,
                    status="2",
                    latest_version="1",
                    updated_at=(
                        "2026-08-02T12:00:00Z"
                        if workspace_id == _SECOND_WORKSPACE_ID
                        else "2026-08-01T12:00:00Z"
                    ),
                )
            ],
            1,
        )

    monkeypatch.setattr(browser_api_module, "list_models", fake_list_models)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "model", "list", "--workspace", "all"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [_WORKSPACE_ID, _SECOND_WORKSPACE_ID]
    assert _json_data(result.output) == {
        "items": [
            {
                "name": "model-serve",
                "status": "SUCCESS",
                "project": "模型项目",
                "workspace": "推理空间",
                "version": "V1",
                "updated_at": "2026-08-02T12:00:00Z",
            },
            {
                "name": "model-train",
                "status": "SUCCESS",
                "project": "模型项目",
                "workspace": "训练空间",
                "version": "V1",
                "updated_at": "2026-08-01T12:00:00Z",
            },
        ],
    }
    for secret in (
        _WORKSPACE_ID,
        _SECOND_WORKSPACE_ID,
        "model-secret-train",
        "model-secret-serve",
    ):
        assert secret not in result.output

    human = CliRunner().invoke(
        cli_main,
        ["model", "list", "--workspace", "all"],
    )
    assert human.exit_code == 0, human.output
    assert "Workspace" in human.output
    assert "训练空间" in human.output
    assert "推理空间" in human.output
    assert _WORKSPACE_ID not in human.output
    assert _SECOND_WORKSPACE_ID not in human.output


@pytest.mark.parametrize(
    ("args", "metavar"),
    (
        (["model", "list", "--help"], "NAME|all"),
        (["model", "status", "--help"], "NAME"),
        (["model", "versions", "--help"], "NAME"),
        (["model", "register", "--help"], "NAME"),
    ),
)
def test_model_workspace_metavars_are_name_only(
    args: list[str],
    metavar: str,
) -> None:
    result = CliRunner().invoke(cli_main, args)

    assert result.exit_code == 0, result.output
    assert f"--workspace {metavar}" in result.output
    assert "--workspace TEXT" not in result.output


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
            "user_name": "253108120116",
            "username": "usr_391",
            "login_name": "student-42",
            "owner": {"name": "Alice", "id": "user-secret-456"},
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


def test_model_list_omits_login_scalars_without_display_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    leaks = ("253108120116", "usr_391", "student-42")
    monkeypatch.setattr(
        browser_api_module,
        "list_models",
        lambda **kwargs: (
            [
                browser_api_module.ModelInfo(
                    model_id="model-secret-login-only",
                    name="qwen-login-only",
                    project_name="模型项目",
                    status="2",
                    user_name=leaks[0],
                    raw={
                        "user_name": leaks[0],
                        "username": leaks[1],
                        "login_name": leaks[2],
                    },
                )
            ],
            1,
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "model", "list", "--workspace", "训练空间"],
    )

    assert result.exit_code == 0, result.output
    item = _json_data(result.output)["items"][0]
    assert "created_by" not in item
    for leak in leaks:
        assert leak not in result.output


def test_model_status_omits_login_scalars_without_display_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        model_commands_module,
        "_resolve_model_name",
        lambda *args, **kwargs: "model-secret-login-only",
    )
    leaks = ("253108120116", "usr_391", "student-42")
    monkeypatch.setattr(
        browser_api_module,
        "get_model_detail",
        lambda **kwargs: {
            "model": {
                "name": "qwen-login-only",
                "status": 2,
                "user_name": leaks[0],
                "username": leaks[1],
                "login_name": leaks[2],
                "owner": leaks[0],
            },
            "user_name": leaks[0],
            "username": leaks[1],
            "login_name": leaks[2],
        },
    )
    monkeypatch.setattr(
        browser_api_module,
        "list_model_version_records",
        lambda **kwargs: {"list": []},
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "model", "status", "qwen-login-only", "--workspace", "训练空间"],
    )

    assert result.exit_code == 0, result.output
    payload = _json_data(result.output)
    assert "owner" not in payload
    for leak in leaks:
        assert leak not in result.output


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
        "items": [
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


def test_model_status_retries_stale_cached_handle_by_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    resolve_calls: list[bool] = []
    detail_calls: list[str] = []
    invalidated: list[str] = []

    def _resolve(
        _ctx,
        _name,
        *,
        require_live=False,
        **_kwargs,
    ):
        resolve_calls.append(require_live)
        return "model-new" if require_live else "model-old"

    monkeypatch.setattr(model_commands_module, "_resolve_model_name", _resolve)
    monkeypatch.setattr(
        model_commands_module,
        "forget_resource_identity",
        lambda **kwargs: invalidated.append(kwargs["resource_id"]),
    )

    def _detail(**kwargs):
        detail_calls.append(kwargs["model_id"])
        if kwargs["model_id"] == "model-old":
            raise RuntimeError("404 not found")
        return {"model": {"name": "qwen-demo", "status": 2}}

    monkeypatch.setattr(browser_api_module, "get_model_detail", _detail)
    monkeypatch.setattr(
        browser_api_module,
        "list_model_version_records",
        lambda **_kwargs: {"list": []},
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "model", "status", "qwen-demo", "--workspace", "训练空间"],
    )

    assert result.exit_code == 0, result.output
    assert resolve_calls == [False, True]
    assert detail_calls == ["model-old", "model-new"]
    assert invalidated == ["model-old"]


def test_model_versions_retries_stale_cached_handle_by_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    resolve_calls: list[bool] = []
    version_calls: list[str] = []
    invalidated: list[str] = []

    def _resolve(
        _ctx,
        _name,
        *,
        require_live=False,
        **_kwargs,
    ):
        resolve_calls.append(require_live)
        return "model-new" if require_live else "model-old"

    monkeypatch.setattr(model_commands_module, "_resolve_model_name", _resolve)
    monkeypatch.setattr(
        model_commands_module,
        "forget_resource_identity",
        lambda **kwargs: invalidated.append(kwargs["resource_id"]),
    )

    def _versions(**kwargs):
        version_calls.append(kwargs["model_id"])
        if kwargs["model_id"] == "model-old":
            raise RuntimeError("model not found")
        return {"list": []}

    monkeypatch.setattr(browser_api_module, "list_model_version_records", _versions)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "model", "versions", "qwen-demo", "--workspace", "训练空间"],
    )

    assert result.exit_code == 0, result.output
    assert resolve_calls == [False, True]
    assert version_calls == ["model-old", "model-new"]
    assert invalidated == ["model-old"]


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

    register_args = [
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
    ]
    result = CliRunner().invoke(
        cli_main,
        ["--json", *register_args],
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

    human_result = CliRunner().invoke(cli_main, register_args)
    assert human_result.exit_code == 0, human_result.output
    assert human_result.output == "OK Model registered: qwen-demo\n"


def test_project_alias_with_platform_id_value_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_module.Config(
        username="user",
        password="pass",
        projects={"production": _WORKSPACE_ID.replace("ws-", "project-")},
    )
    monkeypatch.setattr(browser_api_module, "list_projects", lambda **_kwargs: [])

    with pytest.raises(config_module.ConfigError, match="must map to a project name"):
        model_commands_module._resolve_project_id(
            config,
            "production",
            workspace_id=_WORKSPACE_ID,
            session=_Session(),
        )
