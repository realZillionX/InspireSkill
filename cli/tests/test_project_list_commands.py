import json

from click.testing import CliRunner

from inspire.cli.commands.project import project_commands as project_cmd_module
from inspire.cli.main import main as cli_main
from inspire.cli.utils import notebook_cli as notebook_cli_module
from inspire.platform.web import browser_api as browser_api_module


_FORBIDDEN_PUBLIC_KEYS = {
    "id",
    "project_id",
    "workspace_id",
    "workspace_ids",
    "raw",
    "payload",
    "result",
    "scanned",
    "source",
}


def _json_data(output: str):  # type: ignore[no-untyped-def]
    parsed = json.loads(output)
    return parsed.get("data", parsed)


def _assert_compact_public_payload(value):  # type: ignore[no-untyped-def]
    if isinstance(value, dict):
        for key, child in value.items():
            assert key not in _FORBIDDEN_PUBLIC_KEYS
            assert not key.endswith("_id")
            assert not key.endswith("_ids")
            _assert_compact_public_payload(child)
    elif isinstance(value, list):
        for child in value:
            _assert_compact_public_payload(child)


WS_CPU = "ws-11111111-1111-1111-1111-111111111111"
WS_GPU = "ws-22222222-2222-2222-2222-222222222222"
WS_INET = "ws-33333333-3333-3333-3333-333333333333"
WS_EXTRA = "ws-44444444-4444-4444-4444-444444444444"
WS_BAD = "ws-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
WS_BAD_2 = "ws-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
WS_GOOD = "ws-cccccccc-cccc-cccc-cccc-cccccccccccc"


class FakeSession:
    def __init__(self, *, all_workspace_ids, workspace_id: str | None) -> None:
        self.all_workspace_ids = all_workspace_ids
        self.workspace_id = workspace_id
        self.all_workspace_names = {wid: wid for wid in all_workspace_ids}


def _project(project_id: str, name: str, workspace_id: str) -> browser_api_module.ProjectInfo:
    return browser_api_module.ProjectInfo(
        project_id=project_id,
        name=name,
        workspace_id=workspace_id,
    )


def test_project_list_all_uses_single_project_query(monkeypatch):
    session_obj = FakeSession(
        all_workspace_ids=[WS_CPU, WS_GPU, WS_INET, WS_EXTRA],
        workspace_id=WS_CPU,
    )
    monkeypatch.setattr(
        notebook_cli_module.web_session_module,
        "get_web_session",
        lambda: session_obj,
    )

    calls: list[str] = []

    def fake_list_all_projects(session=None):  # type: ignore[no-untyped-def]
        calls.append("all")
        return [
            browser_api_module.ProjectInfo(
                project_id="project-cpu",
                name="CPU",
                workspace_id=WS_CPU,
                workspace_ids=(WS_CPU,),
                workspace_names=("CPU资源空间",),
            ),
            browser_api_module.ProjectInfo(
                project_id="project-gpu",
                name="GPU",
                workspace_id=WS_GPU,
                workspace_ids=(WS_GPU,),
                workspace_names=("分布式训练空间",),
            ),
            browser_api_module.ProjectInfo(
                project_id="project-extra",
                name="Extra",
                workspace_id=WS_EXTRA,
                workspace_ids=(WS_EXTRA,),
                workspace_names=("专项空间",),
            ),
        ]

    monkeypatch.setattr(browser_api_module, "list_all_projects", fake_list_all_projects)
    monkeypatch.setattr(
        browser_api_module,
        "list_projects",
        lambda **_: (_ for _ in ()).throw(AssertionError("fanout should not run")),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "project", "list", "--workspace", "all"])

    assert result.exit_code == 0
    payload = _json_data(result.output)
    assert len(payload["projects"]) == 3
    assert "total" not in payload
    _assert_compact_public_payload(payload)
    assert "project-cpu" not in result.output
    assert WS_CPU not in result.output
    assert calls == ["all"]


def test_project_list_all_fans_out_when_single_query_lacks_workspace_binding(monkeypatch):
    session_obj = FakeSession(
        all_workspace_ids=[WS_CPU, WS_GPU],
        workspace_id=WS_CPU,
    )
    session_obj.all_workspace_names = {WS_CPU: "CPU资源空间", WS_GPU: "分布式训练空间"}
    monkeypatch.setattr(
        notebook_cli_module.web_session_module,
        "get_web_session",
        lambda: session_obj,
    )

    calls: list[str | None] = []

    def fake_list_all_projects(session=None):  # type: ignore[no-untyped-def]
        calls.append("all")
        return [_project("project-shared", "Shared", "")]

    def fake_list_projects(workspace_id=None, session=None):  # type: ignore[no-untyped-def]
        calls.append(workspace_id)
        return [_project("project-shared", "Shared", workspace_id or "")]

    monkeypatch.setattr(browser_api_module, "list_all_projects", fake_list_all_projects)
    monkeypatch.setattr(browser_api_module, "list_projects", fake_list_projects)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "project", "list", "--workspace", "all"])

    assert result.exit_code == 0
    payload = _json_data(result.output)
    assert len(payload["projects"]) == 1
    assert payload["projects"][0]["workspaces"] == ["CPU资源空间", "分布式训练空间"]
    _assert_compact_public_payload(payload)
    assert calls == ["all", WS_CPU, WS_GPU]


def test_project_list_tolerates_workspace_specific_failure(monkeypatch):
    session_obj = FakeSession(
        all_workspace_ids=[WS_BAD, WS_GOOD],
        workspace_id=WS_GOOD,
    )
    monkeypatch.setattr(
        notebook_cli_module.web_session_module,
        "get_web_session",
        lambda: session_obj,
    )

    calls: list[str | None] = []

    def fake_list_projects(workspace_id=None, session=None):  # type: ignore[no-untyped-def]
        calls.append(workspace_id)
        if workspace_id == WS_BAD:
            raise ValueError("workspace not found")
        if workspace_id == WS_GOOD:
            return [_project("project-good", "Good", WS_GOOD)]
        return []

    monkeypatch.setattr(browser_api_module, "list_projects", fake_list_projects)
    monkeypatch.setattr(
        browser_api_module,
        "list_all_projects",
        lambda **_: (_ for _ in ()).throw(ValueError("single query unavailable")),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "project", "list", "--workspace", "all"])

    assert result.exit_code == 0
    payload = _json_data(result.output)
    assert len(payload["projects"]) == 1
    assert "project_id" not in payload["projects"][0]
    assert payload["projects"][0]["name"] == "Good"
    _assert_compact_public_payload(payload)
    assert calls == [WS_BAD, WS_GOOD]


def test_project_list_does_not_fallback_to_default_query_when_all_workspace_queries_fail(monkeypatch):
    session_obj = FakeSession(
        all_workspace_ids=[WS_BAD, WS_BAD_2],
        workspace_id=WS_GOOD,
    )
    monkeypatch.setattr(
        notebook_cli_module.web_session_module,
        "get_web_session",
        lambda: session_obj,
    )

    calls: list[str | None] = []

    def fake_list_projects(workspace_id=None, session=None):  # type: ignore[no-untyped-def]
        calls.append(workspace_id)
        raise ValueError("workspace denied")

    monkeypatch.setattr(browser_api_module, "list_projects", fake_list_projects)
    monkeypatch.setattr(
        browser_api_module,
        "list_all_projects",
        lambda **_: (_ for _ in ()).throw(ValueError("single query unavailable")),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "project", "list", "--workspace", "all"])

    assert result.exit_code != 0
    assert calls == [WS_BAD, WS_BAD_2]


def test_project_list_specific_workspace_uses_workspace_query(monkeypatch):
    session_obj = FakeSession(
        all_workspace_ids=[WS_BAD, WS_GOOD, WS_CPU, WS_GPU],
        workspace_id=WS_GOOD,
    )
    session_obj.all_workspace_names = {WS_GOOD: "good", WS_BAD: "bad", WS_CPU: "cpu", WS_GPU: "gpu"}
    monkeypatch.setattr(
        notebook_cli_module.web_session_module,
        "get_web_session",
        lambda: session_obj,
    )
    calls: list[str | None] = []

    def fake_list_projects(workspace_id=None, session=None):  # type: ignore[no-untyped-def]
        calls.append(workspace_id)
        return [_project(f"project-{workspace_id}", workspace_id, workspace_id)]

    monkeypatch.setattr(browser_api_module, "list_projects", fake_list_projects)
    monkeypatch.setattr(
        browser_api_module,
        "list_all_projects",
        lambda **_: (_ for _ in ()).throw(AssertionError("all query should not run")),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "project", "list", "--workspace", "good"])

    assert result.exit_code == 0
    payload = _json_data(result.output)
    assert len(payload["projects"]) == 1
    assert "total" not in payload
    _assert_compact_public_payload(payload)
    assert calls == [WS_GOOD]


def test_project_list_human_output_uses_workspace_table(monkeypatch):
    session_obj = FakeSession(
        all_workspace_ids=[WS_GOOD],
        workspace_id=WS_GOOD,
    )
    session_obj.all_workspace_names = {WS_GOOD: "CI-情境智能"}
    monkeypatch.setattr(
        notebook_cli_module.web_session_module,
        "get_web_session",
        lambda: session_obj,
    )

    def fake_list_projects(workspace_id=None, session=None):  # type: ignore[no-untyped-def]
        assert workspace_id == WS_GOOD
        return [
            browser_api_module.ProjectInfo(
                project_id="project-good",
                name="专项项目-2",
                workspace_id=WS_GOOD,
                workspace_ids=(WS_GOOD,),
                priority_level="HIGH",
                member_remain_budget=1234.0,
            )
        ]

    monkeypatch.setattr(browser_api_module, "list_projects", fake_list_projects)
    monkeypatch.setattr(
        browser_api_module,
        "list_all_projects",
        lambda **_: (_ for _ in ()).throw(AssertionError("all query should not run")),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["project", "list", "--workspace", "CI-情境智能"])

    assert result.exit_code == 0
    assert "Projects" not in result.output
    assert "Workspace" in result.output
    assert "专项项目-2" in result.output
    assert "CI-情境智能" in result.output
    assert "1,234" in result.output
    assert "Total:" not in result.output
    assert "project-good" not in result.output


def test_project_list_all_fallback_bypasses_fanout_limit(monkeypatch):
    session_obj = FakeSession(
        all_workspace_ids=[WS_BAD, WS_GOOD, WS_CPU, WS_GPU],
        workspace_id=WS_GOOD,
    )
    monkeypatch.setattr(
        notebook_cli_module.web_session_module,
        "get_web_session",
        lambda: session_obj,
    )
    monkeypatch.setattr(project_cmd_module, "_PROJECT_LIST_WORKSPACE_FANOUT_LIMIT", 2)
    monkeypatch.setattr(project_cmd_module, "_PROJECT_LIST_MAX_WORKERS", 1)

    calls: list[str | None] = []

    def fake_list_projects(workspace_id=None, session=None):  # type: ignore[no-untyped-def]
        calls.append(workspace_id)
        if workspace_id is None:
            return []
        return [_project(f"project-{workspace_id}", workspace_id, workspace_id)]

    monkeypatch.setattr(browser_api_module, "list_projects", fake_list_projects)
    monkeypatch.setattr(
        browser_api_module,
        "list_all_projects",
        lambda **_: (_ for _ in ()).throw(ValueError("single query unavailable")),
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "project", "list", "--workspace", "all"])

    assert result.exit_code == 0
    payload = _json_data(result.output)
    assert len(payload["projects"]) == 4
    assert "total" not in payload
    _assert_compact_public_payload(payload)
    assert calls == [WS_BAD, WS_GOOD, WS_CPU, WS_GPU]


def test_project_list_refreshes_platform_for_all_workspaces(monkeypatch):
    session_obj = FakeSession(
        all_workspace_ids=[WS_BAD, WS_GOOD, WS_CPU, WS_GPU],
        workspace_id=WS_GOOD,
    )
    monkeypatch.setattr(
        notebook_cli_module.web_session_module,
        "get_web_session",
        lambda: session_obj,
    )
    monkeypatch.setattr(project_cmd_module, "_PROJECT_LIST_MAX_WORKERS", 1)

    calls: list[str] = []

    def fake_list_all_projects(session=None):  # type: ignore[no-untyped-def]
        calls.append("all")
        return [_project("project-live", "Live", WS_GOOD)]

    monkeypatch.setattr(browser_api_module, "list_all_projects", fake_list_all_projects)

    runner = CliRunner()
    first = runner.invoke(cli_main, ["--json", "project", "list", "--workspace", "all"])
    second = runner.invoke(cli_main, ["--json", "project", "list", "--workspace", "all"])

    assert first.exit_code == 0
    assert second.exit_code == 0
    first_payload = _json_data(first.output)
    second_payload = _json_data(second.output)
    assert len(first_payload["projects"]) == 1
    assert len(second_payload["projects"]) == 1
    assert "total" not in first_payload
    assert "total" not in second_payload
    _assert_compact_public_payload(first_payload)
    _assert_compact_public_payload(second_payload)
    assert calls == ["all", "all"]


def test_project_detail_json_is_name_only_and_compact(monkeypatch):
    session_obj = FakeSession(all_workspace_ids=[WS_GOOD], workspace_id=WS_GOOD)
    session_obj.all_workspace_names = {WS_GOOD: "研发空间"}
    monkeypatch.setattr(
        notebook_cli_module.web_session_module,
        "get_web_session",
        lambda: session_obj,
    )
    monkeypatch.setattr(
        project_cmd_module,
        "_resolve_project_name",
        lambda *args, **kwargs: "project-secret-123",
    )
    monkeypatch.setattr(
        browser_api_module,
        "get_project_detail",
        lambda project_id, session=None: {
            "id": project_id,
            "project_id": project_id,
            "workspace_id": WS_GOOD,
            "name": "视觉模型项目",
            "en_name": "vision-models",
            "description": "Production models",
            "budget": 1000,
            "remain_budget": 750,
            "priority_name": "HIGH",
            "creator": {"id": "user-secret-123", "name": "Alice"},
            "raw": {"trace": "secret"},
        },
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "project",
            "detail",
            "视觉模型项目",
            "--workspace",
            "研发空间",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = _json_data(result.output)
    assert payload == {
        "name": "视觉模型项目",
        "english_name": "vision-models",
        "description": "Production models",
        "budget": 1000,
        "remaining_budget": 750,
        "priority": "HIGH",
        "creator": "Alice",
    }
    _assert_compact_public_payload(payload)
    assert "project-secret-123" not in result.output
    assert WS_GOOD not in result.output
    assert "user-secret-123" not in result.output


def test_project_detail_retries_stale_cached_handle_by_name(monkeypatch):
    session_obj = FakeSession(all_workspace_ids=[WS_GOOD], workspace_id=WS_GOOD)
    session_obj.all_workspace_names = {WS_GOOD: "研发空间"}
    monkeypatch.setattr(
        notebook_cli_module.web_session_module,
        "get_web_session",
        lambda: session_obj,
    )

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
        return "project-new" if require_live else "project-old"

    monkeypatch.setattr(project_cmd_module, "_resolve_project_name", _resolve)
    monkeypatch.setattr(
        project_cmd_module,
        "forget_resource_identity",
        lambda **kwargs: invalidated.append(kwargs["resource_id"]),
    )

    def _detail(project_id, session=None):
        detail_calls.append(project_id)
        if project_id == "project-old":
            raise RuntimeError("project not found")
        return {"name": "视觉模型项目"}

    monkeypatch.setattr(browser_api_module, "get_project_detail", _detail)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "project",
            "detail",
            "视觉模型项目",
            "--workspace",
            "研发空间",
        ],
    )

    assert result.exit_code == 0, result.output
    assert resolve_calls == [False, True]
    assert detail_calls == ["project-old", "project-new"]
    assert invalidated == ["project-old"]


def test_project_detail_omits_nested_budget_and_text_metadata(monkeypatch):
    session_obj = FakeSession(all_workspace_ids=[WS_GOOD], workspace_id=WS_GOOD)
    session_obj.all_workspace_names = {WS_GOOD: "研发空间"}
    monkeypatch.setattr(
        notebook_cli_module.web_session_module,
        "get_web_session",
        lambda: session_obj,
    )
    monkeypatch.setattr(
        project_cmd_module,
        "_resolve_project_name",
        lambda *args, **kwargs: "project-secret-123",
    )
    monkeypatch.setattr(
        browser_api_module,
        "get_project_detail",
        lambda project_id, session=None: {
            "name": "视觉模型项目",
            "description": {"payload": {"project_id": project_id}},
            "budget": {"amount": 1000, "quota_id": "quota-secret-1"},
            "remain_budget": ["raw", "metadata"],
            "priority_name": {"value": "HIGH", "id": "priority-secret"},
            "creator": {"name": {"value": "Alice"}, "id": "user-secret"},
        },
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "project",
            "detail",
            "视觉模型项目",
            "--workspace",
            "研发空间",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = _json_data(result.output)
    assert payload == {"name": "视觉模型项目"}
    _assert_compact_public_payload(payload)
    assert "payload" not in result.output
    assert "quota-secret-1" not in result.output
    assert "priority-secret" not in result.output
    assert "user-secret" not in result.output


def test_project_owners_json_drops_raw_owner_metadata(monkeypatch):
    session_obj = FakeSession(all_workspace_ids=[WS_GOOD], workspace_id=WS_GOOD)
    monkeypatch.setattr(
        notebook_cli_module.web_session_module,
        "get_web_session",
        lambda: session_obj,
    )
    monkeypatch.setattr(
        browser_api_module,
        "list_project_owners",
        lambda session=None: [
            {
                "id": "user-secret-456",
                "name": "Alice",
                "extra_info": {
                    "login_name": "alice",
                    "workspace_id": WS_GOOD,
                },
            }
        ],
    )

    result = CliRunner().invoke(cli_main, ["--json", "project", "owners"])

    assert result.exit_code == 0, result.output
    payload = _json_data(result.output)
    assert payload == {"owners": [{"name": "Alice", "login": "alice"}]}
    _assert_compact_public_payload(payload)
    assert "user-secret-456" not in result.output
    assert WS_GOOD not in result.output
