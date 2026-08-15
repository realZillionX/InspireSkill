"""Unit tests for `inspire.cli.commands.serving.serving_commands` rendering.

Focuses on the human-readable table renderer: empty state, full-page total,
and the "Showing X of Y" footer that replaces the misleading `len(rows)`-based
total when the caller is paginating. Complements the wire-format tests in
`test_browser_api_servings.py`.
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from typing import Any

import click
import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.commands.serving import serving as serving_group
from inspire.cli.commands.serving import serving_commands as serving_commands_module
from inspire.cli.commands.serving.serving_commands import (
    _build_resource_spec_price,
    _format_configs,
    _format_list_rows,
    _serving_resource_label,
)
from inspire.cli.commands.serving.public_output import public_serving
from inspire.cli.context import EXIT_VALIDATION_ERROR
from inspire.cli.main import main as cli_main
from inspire.cli.utils.collection_output import DEFAULT_COLLECTION_LIMIT
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api.servings import ServingInfo

_REAL_RESOLVE_SERVING_NAME = serving_commands_module._resolve_serving_name


def _workspace_metavars(group: click.Group) -> dict[str, str | None]:
    values: dict[str, str | None] = {}

    def walk(command: click.Command, path: tuple[str, ...]) -> None:
        for parameter in command.params:
            if isinstance(parameter, click.Option) and "--workspace" in parameter.opts:
                values[" ".join(path)] = parameter.metavar
        if isinstance(command, click.Group):
            for name, child in command.commands.items():
                walk(child, (*path, name))

    walk(group, ())
    return values


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
    assert out.splitlines()[0].startswith("Name")
    assert "model=" not in out
    assert "replicas=" not in out
    assert "project=" not in out
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


def test_serving_configs_single_workspace_keeps_compact_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_module.Config(username="user", password="pass")
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(serving_commands_module, "get_web_session", lambda: FakeSession())
    monkeypatch.setattr(
        browser_api_module,
        "get_serving_configs",
        lambda **_: {
            "configs": {
                "enable_auto_stop": True,
                "items": [
                    {
                        "id": "config-secret",
                        "name": "gpu-choice",
                        "gpu_count_min": 1,
                        "gpu_count_max": 2,
                    }
                ],
            }
        },
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "serving", "configs", "--workspace", "Serving空间"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"] == {
        "items": [
            {
                "name": "gpu-choice",
                "gpu_count_min": 1,
                "gpu_count_max": 2,
                "auto_stop": True,
            }
        ],
    }
    assert "workspace" not in result.output
    assert "config-secret" not in result.output


def test_serving_configs_workspace_all_fans_out_with_workspace_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_module.Config(username="user", password="pass")

    class _AllWorkspaceSession:
        storage_state: dict[str, Any] = {}
        all_workspace_ids = ["ws-a", "ws-b"]
        all_workspace_names = {"ws-a": "Serving East", "ws-b": "Serving West"}

    calls: list[str] = []
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(
        serving_commands_module,
        "get_web_session",
        lambda: _AllWorkspaceSession(),
    )

    def fake_configs(*, workspace_id, session=None):  # noqa: ANN001,ARG001
        calls.append(workspace_id)
        return {
            "configs": {
                "enable_auto_stop": workspace_id == "ws-a",
                "items": [
                    {
                        "id": f"config-secret-{workspace_id}",
                        "name": f"choice-{workspace_id[-1]}",
                        "gpu_count_min": 1,
                        "gpu_count_max": 2,
                    }
                ],
            }
        }

    monkeypatch.setattr(browser_api_module, "get_serving_configs", fake_configs)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "serving", "configs", "--workspace", "all"],
    )

    assert result.exit_code == 0, result.output
    assert calls == ["ws-a", "ws-b"]
    assert json.loads(result.output)["data"] == {
        "items": [
            {
                "workspace": "Serving East",
                "name": "choice-a",
                "gpu_count_min": 1,
                "gpu_count_max": 2,
                "auto_stop": True,
            },
            {
                "workspace": "Serving West",
                "name": "choice-b",
                "gpu_count_min": 1,
                "gpu_count_max": 2,
                "auto_stop": False,
            },
        ],
    }
    assert "config-secret" not in result.output
    assert "ws-a" not in result.output
    assert "ws-b" not in result.output

    human = CliRunner().invoke(
        cli_main,
        ["serving", "configs", "--workspace", "all"],
    )
    assert human.exit_code == 0, human.output
    assert "Serving East: gpu=1-2, auto-stop=enabled" in human.output
    assert "Serving West: gpu=1-2, auto-stop=disabled" in human.output
    assert "ws-a" not in human.output
    assert "ws-b" not in human.output


def test_serving_status_projection_renders_nested_web_detail() -> None:
    detail = {
        "model": {"name": "demo-model", "version": 1, "id": "model-hidden"},
        "mirror": {"name": "sandbox-base", "version": "ubuntu24.04"},
        "logic_compute_group": {
            "id": "compute-hidden",
            "name": "H200 Room",
        },
        "created_by": {
            "id": "user-hidden",
            "name": "Alice",
        },
        "resource_spec_price": {
            "cpu_count": 18,
            "memory_size_gib": 200,
            "gpu_count": 1,
            "gpu_info": {"gpu_type_display": "NVIDIA H200"},
        },
    }

    public = public_serving(detail, fallback_name="demo")
    assert public["name"] == "demo"
    assert public["model"] == "demo-model v1"
    assert public["image"] == "sandbox-base:ubuntu24.04"
    assert public["compute_group"] == "H200 Room"
    assert public["created_by"] == "Alice"
    assert _serving_resource_label(detail) == "18 CPU, 200 GiB, 1 GPU (NVIDIA H200)"
    assert "model-hidden" not in json.dumps(public)
    assert "compute-hidden" not in json.dumps(public)
    assert "user-hidden" not in json.dumps(public)


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
    workspace_id = "ws-1"
    all_workspace_names = {"ws-1": "Serving空间"}
    all_workspace_ids = ["ws-1"]


def _patch_serving_cli_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resolved_id: str = "sv-internal",
) -> list[dict[str, Any]]:
    config = config_module.Config(username="user", password="pass")
    resolutions: list[dict[str, Any]] = []
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(serving_commands_module, "get_web_session", lambda: FakeSession())
    monkeypatch.setattr(
        serving_commands_module,
        "_resolve_workspace_id",
        lambda _workspace: "ws-internal",
    )

    def _resolve(
        _ctx,
        name,
        *,
        workspace_id=None,
        pick=None,
        require_live=False,
    ):
        resolutions.append(
            {
                "name": name,
                "workspace_id": workspace_id,
                "pick": pick,
                "require_live": require_live,
            }
        )
        return resolved_id

    monkeypatch.setattr(serving_commands_module, "_resolve_serving_name", _resolve)
    return resolutions


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
        lambda workspace: "ws-1",
    )
    monkeypatch.setattr(
        serving_commands_module,
        "_resolve_serving_name",
        lambda ctx, name, workspace_id=None, pick=None, require_live=False: "sv-1",
    )

    def fake_delete_serving(*, inference_serving_id: str, session=None) -> dict[str, Any]:
        calls["serving_id"] = inference_serving_id
        return {"ok": True}

    monkeypatch.setattr(browser_api_module, "delete_serving", fake_delete_serving)
    return calls


def test_serving_list_all_expands_and_limit_conflict_is_pre_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_module.Config(username="user", password="pass")
    calls: list[int] = []
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(serving_commands_module, "get_web_session", lambda: FakeSession())
    monkeypatch.setattr(
        serving_commands_module,
        "_resolve_workspace_id",
        lambda _workspace: "ws-1",
    )

    def fake_list_servings(**kwargs):  # noqa: ANN001
        calls.append(kwargs["page_size"])
        count = kwargs["page_size"]
        return (
            [
                ServingInfo(
                    inference_serving_id=f"sv-{index:08d}",
                    name=f"demo-{index}",
                    status="RUNNING",
                    model_name="demo-model",
                    model_version="1",
                    project_name="demo-project",
                    workspace_id=kwargs["workspace_id"],
                    created_by_name="Alice",
                    raw={
                        "logic_compute_group": {
                            "id": f"compute-{index:08d}",
                            "name": "H200 Room",
                        }
                    },
                )
                for index in range(count)
            ],
            25,
        )

    monkeypatch.setattr(browser_api_module, "list_servings", fake_list_servings)

    limited = CliRunner().invoke(
        cli_main,
        ["--json", "serving", "list", "--workspace", "Serving空间"],
    )

    assert limited.exit_code == 0, limited.output
    limited_payload = json.loads(limited.output)["data"]
    assert calls == [20]
    assert len(limited_payload["items"]) == 20
    assert limited_payload["shown"] == 20
    assert limited_payload["total"] == 25
    assert limited_payload["truncated"] is True
    assert limited_payload["items"][0] == {
        "name": "demo-0",
        "status": "RUNNING",
        "project": "demo-project",
        "workspace": "Serving空间",
        "compute_group": "H200 Room",
        "created_by": "Alice",
    }
    assert "model" not in limited_payload["items"][0]
    assert "image" not in limited_payload["items"][0]
    assert "updated_at" not in limited_payload["items"][0]
    assert "sv-00000000" not in limited.output
    assert "compute-00000000" not in limited.output

    calls.clear()
    result = CliRunner().invoke(
        cli_main,
        ["--json", "serving", "list", "--workspace", "Serving空间", "--all"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert calls == [20, 25]
    assert len(payload["items"]) == 25
    assert set(payload) == {"items"}

    calls.clear()
    conflict = CliRunner().invoke(
        cli_main,
        [
            "serving",
            "list",
            "--workspace",
            "Serving空间",
            "--all",
            "--limit",
            "3",
        ],
    )
    assert conflict.exit_code != 0
    assert "Use either --limit or --all, not both." in conflict.output
    assert calls == []


def test_serving_list_workspace_all_fans_out_and_uses_visible_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_module.Config(username="user", password="pass")

    class _AllWorkspaceSession:
        storage_state: dict[str, Any] = {}
        all_workspace_ids = ["ws-a", "ws-b"]
        all_workspace_names = {"ws-a": "Serving East", "ws-b": "Serving West"}

    calls: list[str] = []
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(
        serving_commands_module,
        "get_web_session",
        lambda: _AllWorkspaceSession(),
    )

    def fake_list_servings(**kwargs):  # noqa: ANN001
        workspace_id = kwargs["workspace_id"]
        calls.append(workspace_id)
        return (
            [
                ServingInfo(
                    inference_serving_id=f"serving-{workspace_id}",
                    name=f"serving-{workspace_id[-1]}",
                    status="RUNNING",
                    model_name="demo-model",
                    model_version="1",
                    project_name="demo-project",
                    workspace_id=workspace_id,
                    created_by_name="Alice",
                    raw={
                        "logic_compute_group": {
                            "id": f"compute-{workspace_id}",
                            "name": "H200 Room",
                        }
                    },
                )
            ],
            1,
        )

    monkeypatch.setattr(browser_api_module, "list_servings", fake_list_servings)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "serving", "list", "--workspace", "all"],
    )

    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)["data"]["items"]
    assert calls == ["ws-a", "ws-b"]
    assert all(
        set(row)
        == {
            "name",
            "status",
            "project",
            "workspace",
            "compute_group",
            "created_by",
        }
        for row in rows
    )
    assert {row["workspace"] for row in rows} == {
        "Serving East",
        "Serving West",
    }
    assert {row["compute_group"] for row in rows} == {"H200 Room"}
    assert {row["created_by"] for row in rows} == {"Alice"}
    assert "workspace_id" not in result.output
    assert "compute-ws-a" not in result.output
    assert "compute-ws-b" not in result.output


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
        lambda _workspace: "ws-1",
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
    assert "only accept serving names" in result.output
    assert "handle" not in result.output.lower()


@pytest.mark.parametrize(
    "subcommand",
    ("start", "events", "instances", "scale", "versions", "rollback", "api-metrics"),
)
def test_new_serving_commands_share_name_workspace_and_pick_help(
    subcommand: str,
) -> None:
    result = CliRunner().invoke(cli_main, ["serving", subcommand, "--help"])

    assert result.exit_code == 0, result.output
    assert "[OPTIONS] NAME" in " ".join(result.output.split())
    assert "--workspace NAME" in result.output
    assert "--pick INTEGER" in result.output
    assert "Pick the Nth candidate (1-indexed) when the name is ambiguous." in " ".join(
        result.output.split()
    )
    if subcommand == "events":
        assert "--tail INTEGER" in result.output
        assert "--follow" in result.output
    if subcommand == "instances":
        assert "--limit INTEGER" in result.output
        assert "--all" in result.output


def test_serving_workspace_metavars_are_name_oriented() -> None:
    metavars = _workspace_metavars(serving_group)

    assert metavars
    assert {
        path
        for path, metavar in metavars.items()
        if metavar == "NAME|all"
    } == {"list", "configs", "quota"}
    assert all(
        metavar == "NAME"
        for path, metavar in metavars.items()
        if path not in {"list", "configs", "quota"}
    )


@pytest.mark.parametrize(
    ("subcommand", "api_name"),
    (
        ("start", "start_serving"),
        ("events", "list_serving_events"),
        ("instances", "list_serving_instances"),
        ("scale", "scale_serving"),
        ("versions", "list_serving_versions"),
    ),
)
def test_new_serving_commands_reject_raw_handle_before_api(
    monkeypatch: pytest.MonkeyPatch,
    subcommand: str,
    api_name: str,
) -> None:
    monkeypatch.setattr(
        browser_api_module,
        api_name,
        lambda *_args, **_kwargs: pytest.fail(
            "raw handle must be rejected before the Browser API call"
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "serving",
            subcommand,
            "sv-12345678",
            "--workspace",
            "Test Workspace",
            *(["--replicas", "2"] if subcommand == "scale" else []),
        ],
    )

    assert result.exit_code != 0
    assert "only accept serving names" in result.output
    assert "sv-12345678" not in result.output


def test_serving_start_is_name_only_and_forwards_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions = _patch_serving_cli_deps(monkeypatch)
    monkeypatch.setattr(
        serving_commands_module,
        "require_confirmation",
        lambda *_args, **_kwargs: pytest.fail("start must not require confirmation"),
    )
    calls: list[str] = []

    def _start(*, inference_serving_id: str, session) -> dict[str, Any]:  # noqa: ANN001
        calls.append(inference_serving_id)
        return {"inference_serving_id": inference_serving_id, "ok": True}

    monkeypatch.setattr(browser_api_module, "start_serving", _start)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "serving",
            "start",
            "demo",
            "--workspace",
            "Test Workspace",
            "--pick",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == ["sv-internal"]
    assert resolutions == [
        {
            "name": "demo",
            "workspace_id": "ws-internal",
            "pick": 2,
            "require_live": True,
        }
    ]
    assert json.loads(result.output)["data"] == {
        "name": "demo",
        "status": "started",
    }
    assert "sv-internal" not in result.output

    human = CliRunner().invoke(
        cli_main,
        ["serving", "start", "demo", "--workspace", "Test Workspace"],
    )
    assert human.exit_code == 0, human.output
    assert human.output == "OK Serving started: demo\n"
    assert "sv-internal" not in human.output


def test_serving_stop_never_requires_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions = _patch_serving_cli_deps(monkeypatch)
    monkeypatch.setattr(
        serving_commands_module,
        "require_confirmation",
        lambda *_args, **_kwargs: pytest.fail("stop must not require confirmation"),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        browser_api_module,
        "stop_serving",
        lambda *, inference_serving_id, session: calls.append(inference_serving_id),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "serving",
            "stop",
            "demo",
            "--workspace",
            "Test Workspace",
            "--pick",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == ["sv-internal"]
    assert resolutions == [
        {
            "name": "demo",
            "workspace_id": "ws-internal",
            "pick": 2,
            "require_live": True,
        }
    ]
    assert json.loads(result.output)["data"] == {
        "name": "demo",
        "status": "stopped",
    }
    assert "sv-internal" not in result.output


def test_serving_events_use_shared_public_projection_and_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions = _patch_serving_cli_deps(monkeypatch)
    calls: list[str] = []

    def _events(serving_id: str, *, pod_names=None, session=None) -> list[dict[str, Any]]:  # noqa: ANN001
        calls.append(serving_id)
        if pod_names is not None:
            return [
                {
                    "object_id": pod_names[0],
                    "object_type": "INFERENCE_SERVING_INSTANCE",
                    "reason": "Scheduled",
                    "message": "Successfully assigned",
                    "last_timestamp": "2026-08-05 10:00:01",
                }
            ]
        return [
            {
                "object_id": "sv-deadbeef",
                "object_type": "INFERENCE_SERVING",
                "type": "Warning",
                "reason": "FailedScheduling",
                "message": "Could not place sv-deadbeef on pod-cafebabe.",
                "count": 2,
                "last_timestamp": "2026-08-05 10:00:00",
            }
        ]

    monkeypatch.setattr(browser_api_module, "list_serving_events", _events)
    monkeypatch.setattr(
        browser_api_module,
        "list_serving_instances",
        lambda _sid, **_kwargs: (
            [{"name": "frontiers/sv-ed52f184-b66b-478a-8620-379033c6dbf3-0", "rank": 0}],
            1,
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "serving",
            "events",
            "demo",
            "--workspace",
            "Test Workspace",
            "--pick",
            "3",
        ],
    )

    assert result.exit_code == 0, result.output
    # Two calls: the deployment view and the replica view.
    assert calls == ["sv-internal", "sv-internal"]
    assert resolutions == [
        {
            "name": "demo",
            "workspace_id": "ws-internal",
            "pick": 3,
            "require_live": False,
        }
    ]
    payload = json.loads(result.output)["data"]
    assert payload["items"] == [
        {
            "time": "2026-08-05 10:00:00",
            "type": "Warning",
            "reason": "FailedScheduling",
            "message": "Could not place <redacted> on pod-cafebabe.",
            "count": 2,
        },
        {
            "time": "2026-08-05 10:00:01",
            "instance": "rank=0",
            "reason": "Scheduled",
            "message": "Successfully assigned",
        },
    ]
    assert "object_id" not in result.output
    assert "sv-deadbeef" not in result.output


def test_serving_instances_are_bounded_projected_and_name_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions = _patch_serving_cli_deps(monkeypatch)
    calls: list[tuple[str, int]] = []

    def _instances(
        serving_id: str,
        *,
        page: int,
        page_size: int,
        session,
    ) -> tuple[list[dict[str, Any]], int]:  # noqa: ANN001
        assert page == 1
        calls.append((serving_id, page_size))
        return (
            [
                {
                    "pod_id": "pod-deadbeef",
                    "inference_serving_id": "sv-deadbeef",
                    "name": "demo-worker",
                    "instance_status": "RUNNING",
                    "instance_type": "worker",
                    "resource_spec_price": {
                        "cpu_count": 4,
                        "memory_size_gib": 32,
                        "gpu_count": 1,
                    },
                    "created_at": "2026-08-05 09:30:00",
                    "backend": "browser",
                }
            ],
            2,
        )

    monkeypatch.setattr(browser_api_module, "list_serving_instances", _instances)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "serving",
            "instances",
            "demo",
            "--workspace",
            "Test Workspace",
            "--pick",
            "4",
            "--limit",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("sv-internal", 1)]
    assert resolutions == [
        {
            "name": "demo",
            "workspace_id": "ws-internal",
            "pick": 4,
            "require_live": False,
        }
    ]
    assert json.loads(result.output)["data"] == {
        "name": "demo",
        "items": [
            {
                "name": "demo-worker",
                "status": "RUNNING",
                "role": "worker",
                "resource": "4 CPU, 32 GiB, 1 GPU",
                "rank": 0,
            }
        ],
        "total": 2,
        "shown": 1,
        "truncated": True,
    }
    assert "pod-deadbeef" not in result.output
    assert "sv-deadbeef" not in result.output
    assert "backend" not in result.output

    human = CliRunner().invoke(
        cli_main,
        [
            "serving",
            "instances",
            "demo",
            "--workspace",
            "Test Workspace",
            "--limit",
            "1",
        ],
    )
    assert human.exit_code == 0, human.output
    assert human.output.splitlines()[0].lstrip().startswith("Name")
    assert "demo-worker" in human.output
    assert "RUNNING" in human.output
    assert "worker" in human.output
    assert "4 CPU, 32 GiB, 1 GPU" in human.output
    assert "Total:" not in human.output
    assert "Showing 1 of 2. Use --all for the full list." in human.output
    assert "pod-deadbeef" not in human.output
    assert "sv-deadbeef" not in human.output
    assert "2026-08-05 09:30:00" not in human.output


def test_serving_instances_all_refetches_and_conflict_is_pre_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_serving_cli_deps(monkeypatch)
    calls: list[int] = []

    def _instances(
        _serving_id: str,
        *,
        page: int,
        page_size: int,
        session,
    ) -> tuple[list[dict[str, Any]], int]:  # noqa: ANN001
        assert page == 1
        calls.append(page_size)
        count = 1 if page_size == DEFAULT_COLLECTION_LIMIT else 3
        return (
            [
                {
                    "name": f"replica-{index}",
                    "status": "RUNNING",
                    "pod_id": f"pod-{index:08x}",
                }
                for index in range(count)
            ],
            3,
        )

    monkeypatch.setattr(browser_api_module, "list_serving_instances", _instances)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "serving",
            "instances",
            "demo",
            "--workspace",
            "Test Workspace",
            "--all",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert calls == [DEFAULT_COLLECTION_LIMIT, 3]
    assert set(payload) == {"name", "items"}
    assert payload["name"] == "demo"
    assert len(payload["items"]) == 3
    assert all(
        set(item) <= {"name", "status", "role", "type", "resource", "rank"}
        for item in payload["items"]
    )

    calls.clear()
    conflict = CliRunner().invoke(
        cli_main,
        [
            "serving",
            "instances",
            "demo",
            "--workspace",
            "Test Workspace",
            "--limit",
            "1",
            "--all",
        ],
    )
    assert conflict.exit_code != 0
    assert "Use either --limit or --all, not both." in conflict.output
    assert calls == []


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
    assert result.output == "OK Serving deleted: demo\n"


def test_serving_delete_json_requires_yes_before_remote_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(
            lambda cls, require_credentials=True: pytest.fail(
                "config must not load before confirmation"
            )
        ),
    )
    monkeypatch.setattr(
        serving_commands_module,
        "get_web_session",
        lambda: pytest.fail("session must not load before confirmation"),
    )
    monkeypatch.setattr(
        serving_commands_module,
        "_resolve_serving_name",
        lambda *_args, **_kwargs: pytest.fail(
            "resolver must not run before confirmation"
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "serving",
            "delete",
            "demo",
            "--workspace",
            "Test Workspace",
        ],
    )

    assert result.exit_code == EXIT_VALIDATION_ERROR
    payload = json.loads(result.output)
    assert payload["error"]["type"] == "ConfirmationRequired"
    assert payload["error"]["hint"] == "Pass --yes to confirm."


def test_serving_status_retries_stale_cached_handle_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_module.Config(username="user", password="pass")
    calls: list[object] = []
    invalidated: list[str] = []

    monkeypatch.setattr(
        serving_commands_module,
        "require_confirmation",
        lambda *_args, **_kwargs: pytest.fail("status must not require confirmation"),
    )
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )
    monkeypatch.setattr(serving_commands_module, "get_web_session", lambda: FakeSession())
    monkeypatch.setattr(
        serving_commands_module,
        "_resolve_workspace_id",
        lambda _workspace: "ws-1",
    )

    def _resolve(
        _ctx,
        name,
        *,
        workspace_id=None,
        pick=None,
        require_live=False,
    ):
        calls.append(("resolve", name, require_live))
        return "sv-new" if require_live else "sv-old"

    monkeypatch.setattr(serving_commands_module, "_resolve_serving_name", _resolve)
    monkeypatch.setattr(
        serving_commands_module,
        "forget_resource_identity",
        lambda **kwargs: invalidated.append(kwargs["resource_id"]),
    )

    def _detail(*, inference_serving_id, session):
        calls.append(("detail", inference_serving_id))
        if inference_serving_id == "sv-old":
            raise RuntimeError("404 resource not found")
        return {"name": "demo", "status": "RUNNING"}

    monkeypatch.setattr(browser_api_module, "get_serving_detail", _detail)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "serving", "status", "demo", "--workspace", "Test Workspace"],
    )

    assert result.exit_code == 0, result.output
    assert invalidated == ["sv-old"]
    assert calls == [
        ("resolve", "demo", False),
        ("detail", "sv-old"),
        ("resolve", "demo", True),
        ("detail", "sv-new"),
    ]


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


# ---------------------------------------------------------------------------
# scale / versions / rollback
# ---------------------------------------------------------------------------


def test_serving_scale_is_name_only_and_forwards_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions = _patch_serving_cli_deps(monkeypatch)
    calls: dict[str, Any] = {}

    def fake_scale(serving_id: str, *, replica: int, session=None) -> dict[str, Any]:
        calls["serving_id"] = serving_id
        calls["replica"] = replica
        return {}

    monkeypatch.setattr(browser_api_module, "scale_serving", fake_scale)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "serving",
            "scale",
            "demo-svc",
            "--workspace",
            "Serving空间",
            "--replicas",
            "3",
            "--pick",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "success": True,
        "data": {"name": "demo-svc", "status": "scaled", "replicas": 3},
    }
    assert calls == {"serving_id": "sv-internal", "replica": 3}
    # Scaling mutates a live deployment, so the handle has to come from a fresh
    # lookup rather than a possibly stale cache entry.
    assert resolutions[-1]["require_live"] is True
    assert resolutions[-1]["pick"] == 2


def test_serving_scale_human_output_is_compact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_serving_cli_deps(monkeypatch)
    monkeypatch.setattr(
        browser_api_module,
        "scale_serving",
        lambda serving_id, *, replica, session=None: {},
    )

    result = CliRunner().invoke(
        cli_main,
        ["serving", "scale", "demo-svc", "--workspace", "Serving空间", "--replicas", "0"],
    )

    assert result.exit_code == 0, result.output
    assert result.output == "OK Serving scaled to 0 replica(s): demo-svc\n"


def test_serving_scale_requires_a_replica_count() -> None:
    result = CliRunner().invoke(
        cli_main, ["serving", "scale", "demo-svc", "--workspace", "Serving空间"]
    )

    assert result.exit_code == 2
    assert "--replicas" in result.output


def test_serving_versions_are_bounded_and_name_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_serving_cli_deps(monkeypatch)
    versions = [
        {
            "inference_serving_id": "sv-internal",
            "version": index,
            "status": "SUCCEEDED",
            "replicas": 1,
            "created_at": "2026-04-20 10:00:00",
        }
        for index in range(1, 26)
    ]
    monkeypatch.setattr(
        browser_api_module,
        "list_serving_versions",
        lambda serving_id, session=None: (versions, len(versions)),
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "serving", "versions", "demo-svc", "--workspace", "Serving空间"],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["shown"] == DEFAULT_COLLECTION_LIMIT
    assert data["total"] == 25
    assert data["truncated"] is True
    assert data["items"][0]["version"] == 1
    # The rollback target is a version number; the platform handle is not part
    # of the contract.
    assert "sv-internal" not in result.output


def test_serving_rollback_prompts_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_serving_cli_deps(monkeypatch)
    monkeypatch.setattr(
        browser_api_module,
        "rollback_serving",
        lambda *_args, **_kwargs: pytest.fail("rollback must be confirmed first"),
    )

    result = CliRunner().invoke(
        cli_main,
        ["serving", "rollback", "demo-svc", "--workspace", "Serving空间", "--version", "2"],
        input="n\n",
    )

    assert result.exit_code != 0
    assert "version 2" in result.output


def test_serving_rollback_yes_skips_prompt_and_sends_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_serving_cli_deps(monkeypatch)
    calls: dict[str, Any] = {}

    def fake_rollback(serving_id: str, *, version: int, session=None) -> dict[str, Any]:
        calls["serving_id"] = serving_id
        calls["version"] = version
        return {}

    monkeypatch.setattr(browser_api_module, "rollback_serving", fake_rollback)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "serving",
            "rollback",
            "demo-svc",
            "--workspace",
            "Serving空间",
            "--version",
            "2",
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "success": True,
        "data": {"name": "demo-svc", "status": "rolled back", "version": 2},
    }
    assert calls == {"serving_id": "sv-internal", "version": 2}


# ---------------------------------------------------------------------------
# create: auto scaling and read-only public path
# ---------------------------------------------------------------------------


def test_serving_create_help_documents_the_new_optional_flags() -> None:
    result = CliRunner().invoke(cli_main, ["serving", "create", "--help"])

    assert result.exit_code == 0, result.output
    joined = " ".join(result.output.split())
    assert "--auto-scaling / --no-auto-scaling" in joined
    assert "--public-path-readonly / --no-public-path-readonly" in joined
    # Both must read as opt-in; the platform still owns the unset case.
    assert joined.count("Omit to leave the platform default.") >= 2


@pytest.mark.parametrize(
    ("flag", "field"),
    (
        ("--auto-scaling", "enable_auto_scaling"),
        ("--no-auto-scaling", "enable_auto_scaling"),
        ("--public-path-readonly", "is_publicpath_readonly"),
        ("--no-public-path-readonly", "is_publicpath_readonly"),
    ),
)
def test_serving_create_flags_map_onto_the_create_action_fields(
    flag: str, field: str
) -> None:
    parameters = {
        parameter.name: parameter
        for parameter in serving_commands_module.create_serving.params
    }
    option = parameters["auto_scaling" if "auto-scaling" in flag else "public_path_readonly"]

    assert flag in option.secondary_opts or flag in option.opts
    # Default `None` is what keeps an untouched create byte-for-byte unchanged.
    assert option.default is None
    assert field in inspect.signature(browser_api_module.create_serving).parameters


# ---------------------------------------------------------------------------
# published per-quota priority restrictions, enforced before the create call
# ---------------------------------------------------------------------------


def _patch_serving_create_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    allowed_priority_levels: tuple[str, ...] | None,
    priority: int,
) -> None:
    from inspire.cli.utils import quota_resolver as quota_resolver_module

    config = config_module.Config(username="user", password="pass")
    config.profiles = {}
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, **_kwargs: (config, {})),
    )
    monkeypatch.setattr(serving_commands_module, "get_web_session", lambda: FakeSession())
    monkeypatch.setattr(
        serving_commands_module, "select_workspace_id", lambda **_kwargs: "ws-1"
    )
    monkeypatch.setattr(
        serving_commands_module, "_resolve_project_id", lambda **_kwargs: "project-1"
    )
    monkeypatch.setattr(
        serving_commands_module.browser_api_module,
        "get_current_user",
        lambda **_kwargs: {"id": "user-1"},
    )
    monkeypatch.setattr(
        quota_resolver_module,
        "resolve_quota",
        lambda **_kwargs: quota_resolver_module.ResolvedQuota(
            quota_id="quota-1",
            logic_compute_group_id="lcg-1",
            compute_group_name="训练区-H200-1号机房",
            gpu_count=1,
            cpu_count=20,
            memory_gib=200,
            gpu_type="H200",
            raw_price={
                "cpu_info": {"cpu_type": "CPU_TYPE_INTEL"},
                "gpu_info": {"gpu_type": "NVIDIA_H200_SXM_141G"},
            },
            allowed_priority_levels=allowed_priority_levels,
        ),
    )
    monkeypatch.setattr(
        serving_commands_module,
        "_resolve_model_for_create",
        lambda **_kwargs: ("model-1", 3, "qwen-demo"),
    )
    monkeypatch.setattr(
        serving_commands_module,
        "_resolve_image_for_create",
        lambda *_args, **_kwargs: ("mirror-1", "serve-base:v1"),
    )
    monkeypatch.setattr(
        serving_commands_module,
        "resolve_workspace_task_priority",
        lambda *_args, **_kwargs: priority,
    )


def _serving_create_args() -> list[str]:
    return [
        "serving",
        "create",
        "--name",
        "probe",
        "--model",
        "qwen-demo",
        "--command",
        "python serve.py",
        "--port",
        "8000",
        "--workspace",
        "Serving空间",
        "--project",
        "Project One",
        "--group",
        "训练区-H200-1号机房",
        "--quota",
        "1,20,200",
        "--image",
        "serve-base:v1",
        "--dry-run",
    ]


def test_serving_create_refuses_a_priority_the_quota_row_does_not_allow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_serving_create_deps(
        monkeypatch, allowed_priority_levels=("low",), priority=4
    )

    result = CliRunner().invoke(cli_main, _serving_create_args())

    assert result.exit_code == EXIT_VALIDATION_ERROR, result.output
    assert "LOW-priority only" in result.output
    assert "inspire serving quota" in result.output
    assert "Create plan" not in result.output


def test_serving_create_accepts_the_priority_the_quota_row_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_serving_create_deps(
        monkeypatch, allowed_priority_levels=("low",), priority=1
    )

    result = CliRunner().invoke(cli_main, _serving_create_args())

    assert result.exit_code == 0, result.output
    assert "Create plan" in result.output


@pytest.mark.parametrize("levels", [None, ()])
def test_serving_create_is_not_blocked_by_an_unread_or_empty_priority_menu(
    monkeypatch: pytest.MonkeyPatch, levels: tuple[str, ...] | None
) -> None:
    _patch_serving_create_deps(monkeypatch, allowed_priority_levels=levels, priority=4)

    result = CliRunner().invoke(cli_main, _serving_create_args())

    assert result.exit_code == 0, result.output
    assert "Create plan" in result.output
