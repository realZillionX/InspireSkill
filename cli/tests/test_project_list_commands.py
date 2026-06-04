import json

from click.testing import CliRunner

from inspire.cli.commands.project import project_commands as project_cmd_module
from inspire.cli.main import main as cli_main
from inspire.cli.utils import notebook_cli as notebook_cli_module
from inspire.platform.web import browser_api as browser_api_module

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
            _project("project-cpu", "CPU", WS_CPU),
            _project("project-gpu", "GPU", WS_GPU),
            _project("project-extra", "Extra", WS_EXTRA),
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
    payload = json.loads(result.output)["data"]
    assert payload["total"] == 3
    assert calls == ["all"]


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
    payload = json.loads(result.output)["data"]
    assert payload["total"] == 1
    assert "project_id" not in payload["projects"][0]
    assert payload["projects"][0]["name"] == "Good"
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
    payload = json.loads(result.output)["data"]
    assert payload["total"] == 1
    assert calls == [WS_GOOD]


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
    payload = json.loads(result.output)["data"]
    assert payload["total"] == 4
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
    first_payload = json.loads(first.output)["data"]
    second_payload = json.loads(second.output)["data"]
    assert first_payload["total"] == 1
    assert second_payload["total"] == 1
    assert calls == ["all", "all"]
