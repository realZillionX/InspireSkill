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
    # Deployment lookups every `model status` / `model versions` run makes.
    # Individual tests override these; the defaults answer "platform says
    # nothing is deployed", which is not the same as "we did not ask".
    monkeypatch.setattr(
        browser_api_module,
        "get_model_vllm_compatibility",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        browser_api_module,
        "check_model_inference_serving_pending",
        lambda **kwargs: {"has_pending_serving": False},
    )
    monkeypatch.setattr(
        browser_api_module,
        "list_model_inference_servings",
        lambda **kwargs: ([], 0),
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
    monkeypatch.setattr(
        browser_api_module,
        "get_model_vllm_compatibility",
        lambda *args, **kwargs: {4: True},
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
        "pending_serving": False,
        "servings": [],
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
    monkeypatch.setattr(
        browser_api_module,
        "get_model_vllm_compatibility",
        lambda *args, **kwargs: {1: True, 2: False},
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


# ---------------------------------------------------------------------------
# deploy-config
# ---------------------------------------------------------------------------


def _patch_deploy_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    latest_version: str = "3",
    recommended: dict[str, Any] | None = None,
    vllm: bool = True,
) -> dict[str, Any]:
    _patch_runtime(monkeypatch, tmp_path)
    calls: dict[str, Any] = {}

    model = browser_api_module.ModelInfo(
        model_id="model-secret-123",
        name="qwen-demo",
        latest_version=latest_version,
    )
    monkeypatch.setattr(
        browser_api_module,
        "list_models",
        lambda **_kwargs: ([model], 1),
    )
    monkeypatch.setattr(
        model_commands_module,
        "_resolve_model_name",
        lambda *_args, **_kwargs: "model-secret-123",
    )

    def _recommended(model_id, *, version, session=None, workspace_id=None):
        calls["recommended"] = {"model_id": model_id, "version": version}
        return (
            recommended
            if recommended is not None
            else {
                "min_node_count": 1,
                "min_gpu_count_per_node": 1,
                "min_cpu_count_per_node": 2,
                "min_memory_size_gib_per_node": 16,
            }
        )

    def _vllm(model_id, *, version, inference_serving_type="CUSTOM", session=None, workspace_id=None):
        calls["vllm"] = {"model_id": model_id, "version": version}
        return vllm

    monkeypatch.setattr(
        browser_api_module, "get_model_recommended_config", _recommended
    )
    monkeypatch.setattr(browser_api_module, "check_model_vllm_compatible", _vllm)
    return calls


def test_model_deploy_config_json_is_a_serving_create_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_deploy_config(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "model", "deploy-config", "qwen-demo", "--workspace", "训练空间"],
    )

    assert result.exit_code == 0, result.output
    data = _json_data(result.output)
    assert data["model"] == "qwen-demo"
    assert data["version"] == 3
    assert data["vllm_compatible"] is True
    # The floor is reported in the exact spelling `serving create --quota`
    # takes, so an Agent does not have to reassemble it.
    assert data["min_quota"] == "1,2,16"
    assert data["min_nodes"] == 1
    _assert_compact_public_payload(data)
    assert "model-secret-123" not in result.output


def test_model_deploy_config_defaults_to_the_latest_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_deploy_config(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        cli_main, ["model", "deploy-config", "qwen-demo", "--workspace", "训练空间"]
    )

    assert result.exit_code == 0, result.output
    assert calls["recommended"]["version"] == 3
    assert calls["vllm"]["version"] == 3


def test_model_deploy_config_explicit_version_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_deploy_config(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        cli_main,
        [
            "model",
            "deploy-config",
            "qwen-demo",
            "--workspace",
            "训练空间",
            "--version",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["recommended"]["version"] == 1


def test_model_deploy_config_requires_a_version_it_can_resolve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_deploy_config(monkeypatch, tmp_path, latest_version="")

    result = CliRunner().invoke(
        cli_main, ["model", "deploy-config", "qwen-demo", "--workspace", "训练空间"]
    )

    assert result.exit_code != 0
    assert "--version" in result.output


def test_model_deploy_config_omits_a_quota_it_cannot_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A partial floor must not be rendered as a `--quota` triple; a guessed
    # zero would read as "no GPU needed".
    _patch_deploy_config(
        monkeypatch,
        tmp_path,
        recommended={"min_node_count": 2, "min_gpu_count_per_node": 8},
        vllm=False,
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "model", "deploy-config", "qwen-demo", "--workspace", "训练空间"],
    )

    assert result.exit_code == 0, result.output
    data = _json_data(result.output)
    assert "min_quota" not in data
    assert data["min_gpu_per_node"] == 8
    assert data["min_nodes"] == 2
    assert data["vllm_compatible"] is False


def test_model_deploy_config_rejects_a_platform_handle_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser_api_module,
        "get_model_recommended_config",
        lambda *_args, **_kwargs: pytest.fail(
            "raw handle must be rejected before the Browser API call"
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "model",
            "deploy-config",
            "model-12345678-1234-1234-1234-123456789abc",
            "--workspace",
            "训练空间",
        ],
    )

    assert result.exit_code != 0


def _status_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    versions: list[dict[str, Any]],
) -> None:
    """Wire `model status` up to one model whose version records are given."""
    _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        model_commands_module,
        "_resolve_model_name",
        lambda *args, **kwargs: "model-secret-serving",
    )
    monkeypatch.setattr(
        browser_api_module,
        "get_model_detail",
        lambda **kwargs: {"model": {"name": "qwen-demo", "status": 2}},
    )
    monkeypatch.setattr(
        browser_api_module,
        "list_model_version_records",
        lambda **kwargs: {"list": versions, "total": len(versions)},
    )


def test_model_status_names_the_servings_holding_the_reported_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Name and readable status only -- and only the servings still holding it.

    The platform hands back `serving_id`, `user_avatar` and a scalar
    `user_name` beside the name, plus every serving that ever referenced the
    version. A failed serving holds nothing and cannot be restarted, so it is
    not an answer to "is anyone using this".
    """
    _status_runtime(
        monkeypatch,
        tmp_path,
        versions=[{"model": {"version": 4, "status": 2}}],
    )
    requests: list[dict[str, Any]] = []

    def _servings(**kwargs):
        requests.append(kwargs)
        return (
            [
                {
                    "name": "qwen-prod",
                    "serving_id": "sv-secret-1",
                    "status": 4,
                    "user_avatar": "https://avatars.invalid/secret.svg",
                    "user_name": "253108120116",
                    "version": 7,
                },
                {
                    "name": "qwen-rollout",
                    "serving_id": "sv-secret-2",
                    "status": 2,
                    "user_name": "usr_391",
                    "version": 1,
                },
                {
                    "name": "qwen-dead",
                    "serving_id": "sv-secret-3",
                    "status": 3,
                    "user_name": "student-42",
                    "version": 1,
                },
                {
                    "name": "qwen-asleep",
                    "serving_id": "sv-secret-4",
                    "status": 7,
                    "version": 1,
                },
            ],
            4,
        )

    monkeypatch.setattr(browser_api_module, "list_model_inference_servings", _servings)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "model", "status", "qwen-demo", "--workspace", "训练空间"],
    )

    assert result.exit_code == 0, result.output
    payload = _json_data(result.output)
    assert payload["servings"] == [
        {"name": "qwen-prod", "status": "RUNNING"},
        {"name": "qwen-rollout", "status": "DEPLOYING"},
        {"name": "qwen-asleep", "status": "STOPPED"},
    ]
    # The reported version, not the serving's own revision, is what was asked.
    assert requests[0]["version"] == 4
    _assert_compact_public_payload(payload)
    for secret in (
        "sv-secret-1",
        "avatars.invalid",
        "253108120116",
        "usr_391",
        "student-42",
        "qwen-dead",
    ):
        assert secret not in result.output


def test_model_status_human_view_lists_servings_under_their_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _status_runtime(
        monkeypatch,
        tmp_path,
        versions=[{"model": {"version": 4, "status": 2}}],
    )
    monkeypatch.setattr(
        browser_api_module,
        "list_model_inference_servings",
        lambda **kwargs: ([{"name": "qwen-prod", "status": 4}], 1),
    )

    result = CliRunner().invoke(
        cli_main,
        ["model", "status", "qwen-demo", "--workspace", "训练空间"],
    )

    assert result.exit_code == 0, result.output
    assert "Serving on V4: qwen-prod (RUNNING)" in result.output
    assert "Pending deployment: no" in result.output


def test_model_status_says_none_rather_than_going_quiet(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An empty answer is a real answer and has to be printed as one."""
    _status_runtime(
        monkeypatch,
        tmp_path,
        versions=[{"model": {"version": 4, "status": 2}}],
    )

    result = CliRunner().invoke(
        cli_main,
        ["model", "status", "qwen-demo", "--workspace", "训练空间"],
    )

    assert result.exit_code == 0, result.output
    assert "Servings on V4: none" in result.output


def test_model_status_reports_a_pending_deployment_the_running_count_misses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A queued deployment counts as zero running servings on every version.

    This is the case the version records cannot express: `model versions` shows
    `Servings 0` while a deployment is waiting to start on the model.
    """
    _status_runtime(
        monkeypatch,
        tmp_path,
        versions=[
            {"model": {"version": 1, "status": 2}, "running_infrence_serving": "0"}
        ],
    )
    pending_requests: list[dict[str, Any]] = []

    def _pending(**kwargs):
        pending_requests.append(kwargs)
        return {"has_pending_serving": True}

    monkeypatch.setattr(
        browser_api_module, "check_model_inference_serving_pending", _pending
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "model", "status", "qwen-demo", "--workspace", "训练空间"],
    )

    assert result.exit_code == 0, result.output
    assert _json_data(result.output)["pending_serving"] is True
    # Whole-model question: a version would narrow it back to one version.
    assert "version" not in pending_requests[0]


def test_model_status_flags_other_versions_that_still_run_servings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Deleting a model takes every version's deployments, not just the newest."""
    _status_runtime(
        monkeypatch,
        tmp_path,
        versions=[
            {"model": {"version": 1, "status": 2}, "running_infrence_serving": "2"},
            {"model": {"version": 2, "status": 2}, "running_infrence_serving": "0"},
            {"model": {"version": 3, "status": 2}, "running_infrence_serving": 1},
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "model", "status", "qwen-demo", "--workspace", "训练空间"],
    )

    assert result.exit_code == 0, result.output
    payload = _json_data(result.output)
    assert payload["version"] == "V3"
    assert payload["other_versions_in_use"] == ["V1"]


def test_model_status_omits_other_versions_when_only_the_newest_is_used(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _status_runtime(
        monkeypatch,
        tmp_path,
        versions=[
            {"model": {"version": 1, "status": 2}, "running_infrence_serving": "0"},
            {"model": {"version": 2, "status": 2}, "running_infrence_serving": "3"},
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "model", "status", "qwen-demo", "--workspace", "训练空间"],
    )

    assert result.exit_code == 0, result.output
    assert "other_versions_in_use" not in _json_data(result.output)


def test_model_status_bounds_the_serving_list_to_the_output_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _status_runtime(
        monkeypatch,
        tmp_path,
        versions=[{"model": {"version": 1, "status": 2}}],
    )
    monkeypatch.setattr(
        browser_api_module,
        "list_model_inference_servings",
        lambda **kwargs: (
            [{"name": f"svc-{index}", "status": 4} for index in range(25)],
            25,
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "model", "status", "qwen-demo", "--workspace", "训练空间"],
    )

    assert result.exit_code == 0, result.output
    payload = _json_data(result.output)
    assert len(payload["servings"]) == 20
    assert payload["servings_shown"] == 20
    assert payload["servings_total"] == 25
    assert payload["servings_truncated"] is True


def test_model_status_fails_loudly_when_the_serving_lookup_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A refused lookup must never read as "nothing is deployed"."""
    _status_runtime(
        monkeypatch,
        tmp_path,
        versions=[{"model": {"version": 1, "status": 2}}],
    )

    def _refuse(**_kwargs):
        raise ValueError("AccessForbidden: nope")

    monkeypatch.setattr(browser_api_module, "list_model_inference_servings", _refuse)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "model", "status", "qwen-demo", "--workspace", "训练空间"],
    )

    assert result.exit_code != 0
    assert "servings" not in result.output
    assert "APIError" in result.output


def test_model_status_takes_vllm_readiness_from_the_live_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The stored flag on a version record reads false for every model.

    Trusting it made `model status` contradict `model deploy-config`, which has
    always asked the platform.
    """
    _status_runtime(
        monkeypatch,
        tmp_path,
        versions=[{"model": {"version": 2, "status": 2, "is_vllm_compatible": False}}],
    )
    monkeypatch.setattr(
        browser_api_module,
        "get_model_vllm_compatibility",
        lambda *args, **kwargs: {2: True},
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "model", "status", "qwen-demo", "--workspace", "训练空间"],
    )

    assert result.exit_code == 0, result.output
    assert _json_data(result.output)["vllm_ready"] is True


def test_model_status_omits_vllm_readiness_the_platform_did_not_answer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _status_runtime(
        monkeypatch,
        tmp_path,
        versions=[{"model": {"version": 2, "status": 2, "is_vllm_compatible": True}}],
    )
    monkeypatch.setattr(
        browser_api_module,
        "get_model_vllm_compatibility",
        lambda *args, **kwargs: {1: True},
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "model", "status", "qwen-demo", "--workspace", "训练空间"],
    )

    assert result.exit_code == 0, result.output
    assert "vllm_ready" not in _json_data(result.output)


def test_model_versions_takes_vllm_readiness_from_the_live_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        model_commands_module,
        "_resolve_model_name",
        lambda *args, **kwargs: "model-secret-vllm",
    )
    monkeypatch.setattr(
        browser_api_module,
        "list_model_version_records",
        lambda **kwargs: {
            "list": [
                {"model": {"version": 1, "status": 2, "is_vllm_compatible": False}},
                {"model": {"version": 2, "status": 2, "is_vllm_compatible": False}},
            ],
            "total": 2,
        },
    )
    monkeypatch.setattr(
        browser_api_module,
        "get_model_vllm_compatibility",
        lambda *args, **kwargs: {1: True},
    )

    result = CliRunner().invoke(
        cli_main,
        ["model", "versions", "qwen-demo", "--workspace", "训练空间"],
    )

    assert result.exit_code == 0, result.output
    rows = [
        line.split()
        for line in result.output.splitlines()
        if line[:1] == "V" and line[1:2].isdigit()
    ]
    assert rows[0][:2] == ["V1", "SUCCESS"]
    assert rows[0][2] == "yes"
    # V2 was not in the answer; an unknown flag prints as unknown, not as "no".
    assert rows[1][2] == "-"
