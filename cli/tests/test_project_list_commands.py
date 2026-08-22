import json

from click.testing import CliRunner

from inspire.cli.commands.project import project_commands as project_cmd_module
from inspire.cli.main import main as cli_main
from inspire.cli.utils import notebook_cli as notebook_cli_module
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api import projects as projects_module


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


def test_project_info_keeps_member_budget_fallback_and_consumed_fields() -> None:
    project = projects_module._project_info_from_item(
        {
            "id": "project-internal",
            "name": "Demo",
            "en_name": "demo",
            "budget": "20",
            "remain_budget": "12.5",
        }
    )

    assert project.en_name == "demo"
    # No member figure in the record, so the project's stands in for it.
    assert project.member_remain_budget == 12.5
    assert project.remain_budget == 12.5
    # The ceiling is not a remainder and nothing reads it.
    assert "budget" not in project.__dataclass_fields__


def test_project_info_keeps_the_two_budgets_apart() -> None:
    """The member allowance is not the project's, and can be far smaller."""
    project = projects_module._project_info_from_item(
        {
            "id": "project-internal",
            "name": "Demo",
            "remain_budget": "233112.73",
            "member_remain_budget": "337.79",
        }
    )

    assert project.member_remain_budget == 337.79
    assert project.remain_budget == 233112.73


def test_project_list_reads_the_global_catalog_once(monkeypatch):
    """Projects are global, so one unfiltered call is the whole answer."""
    session_obj = FakeSession(
        all_workspace_ids=[WS_GOOD, WS_CPU, WS_GPU],
        workspace_id=WS_GOOD,
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
                project_id="project-good",
                name="专项项目-2",
                workspace_id="",
                priority_level="HIGH",
                member_remain_budget=1234.0,
            )
        ]

    monkeypatch.setattr(browser_api_module, "list_all_projects", fake_list_all_projects)
    monkeypatch.setattr(
        browser_api_module,
        "list_projects",
        lambda **_: (_ for _ in ()).throw(
            AssertionError("a workspace fanout must not run")
        ),
    )

    result = CliRunner().invoke(cli_main, ["--json", "project", "list"])

    assert result.exit_code == 0, result.output
    assert calls == ["all"]
    items = _json_data(result.output)["items"]
    assert [item["name"] for item in items] == ["专项项目-2"]
    _assert_compact_public_payload(items)


def test_project_list_human_output_drops_the_workspace_column(monkeypatch):
    """The column only ever existed to label a fanout's merged rows."""
    session_obj = FakeSession(all_workspace_ids=[WS_GOOD], workspace_id=WS_GOOD)
    session_obj.all_workspace_names = {WS_GOOD: "CI-情境智能"}
    monkeypatch.setattr(
        notebook_cli_module.web_session_module,
        "get_web_session",
        lambda: session_obj,
    )
    monkeypatch.setattr(
        browser_api_module,
        "list_all_projects",
        lambda **_: [
            browser_api_module.ProjectInfo(
                project_id="project-good",
                name="专项项目-2",
                workspace_id=WS_GOOD,
                workspace_ids=(WS_GOOD,),
                priority_level="HIGH",
                member_remain_budget=1234.0,
            )
        ],
    )

    result = CliRunner().invoke(cli_main, ["project", "list"])

    assert result.exit_code == 0, result.output
    assert "Workspace" not in result.output
    assert "专项项目-2" in result.output
    assert "1,234" in result.output
    assert "project-good" not in result.output


def test_project_commands_reject_a_workspace_option() -> None:
    """A workspace filter on a global object can only hide rows, never scope."""
    group = cli_main.commands["project"]
    for name in ("list", "detail", "owners"):
        assert "workspace" not in {
            parameter.name for parameter in group.commands[name].params
        }

    passed = CliRunner().invoke(cli_main, ["project", "list", "--workspace", "any"])
    assert passed.exit_code != 0
    assert "No such option" in passed.output
    assert "--workspace" in passed.output


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
    assert payload == {"items": [{"name": "Alice"}]}
    _assert_compact_public_payload(payload)
    assert "user-secret-456" not in result.output
    assert WS_GOOD not in result.output

    human = CliRunner().invoke(cli_main, ["project", "owners"])
    assert human.exit_code == 0, human.output
    assert "Alice" in human.output
    assert "Login" not in human.output
