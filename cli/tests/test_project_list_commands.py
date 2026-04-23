import json

from click.testing import CliRunner
import pytest

from inspire import config as config_module
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


def _project(project_id: str, name: str, workspace_id: str) -> browser_api_module.ProjectInfo:
    return browser_api_module.ProjectInfo(
        project_id=project_id,
        name=name,
        workspace_id=workspace_id,
    )


@pytest.fixture(autouse=True)
def _isolate_project_cache(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Redirect the project-list cache file into tmp_path."""
    cache_file = tmp_path / "project_list.json"
    monkeypatch.setattr(
        project_cmd_module, "_project_list_cache_file", lambda session: str(cache_file)
    )


def test_project_list_uses_config_workspaces_when_session_discovery_missing(monkeypatch):
    session_obj = FakeSession(all_workspace_ids=None, workspace_id=None)
    monkeypatch.setattr(
        notebook_cli_module.web_session_module,
        "get_web_session",
        lambda: session_obj,
    )

    cfg = config_module.Config(
        username="user",
        password="pass",
        workspace_cpu_id=WS_CPU,
        workspace_gpu_id=WS_GPU,
        workspaces={
            "cpu": WS_CPU,
            "gpu": WS_GPU,
            "internet": WS_INET,
            "extra": WS_EXTRA,
        },
    )
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_target_dir=False, require_credentials=True: (cfg, {})),
    )
    monkeypatch.setattr(project_cmd_module, "_PROJECT_LIST_MAX_WORKERS", 1)

    calls: list[str | None] = []

    def fake_list_projects(workspace_id=None, session=None):  # type: ignore[no-untyped-def]
        calls.append(workspace_id)
        data = {
            WS_CPU: [_project("project-cpu", "CPU", WS_CPU)],
            WS_GPU: [_project("project-gpu", "GPU", WS_GPU)],
            WS_EXTRA: [_project("project-extra", "Extra", WS_EXTRA)],
        }
        return data.get(workspace_id, [])

    monkeypatch.setattr(browser_api_module, "list_projects", fake_list_projects)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "project", "list"])

    assert result.exit_code == 0
    payload = json.loads(result.output)["data"]
    assert payload["total"] == 3
    assert calls == [WS_CPU, WS_GPU, WS_INET, WS_EXTRA]


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

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "project", "list"])

    assert result.exit_code == 0
    payload = json.loads(result.output)["data"]
    assert payload["total"] == 1
    assert payload["projects"][0]["project_id"] == "project-good"
    assert calls == [WS_BAD, WS_GOOD]


def test_project_list_falls_back_to_default_query_when_all_workspace_queries_fail(monkeypatch):
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
        if workspace_id is None:
            return [_project("project-default", "Default", WS_GOOD)]
        raise ValueError("workspace denied")

    monkeypatch.setattr(browser_api_module, "list_projects", fake_list_projects)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "project", "list"])

    assert result.exit_code == 0
    payload = json.loads(result.output)["data"]
    assert payload["total"] == 1
    assert payload["projects"][0]["project_id"] == "project-default"
    assert calls == [WS_BAD, WS_BAD_2, None]


def test_project_list_default_mode_queries_all_workspaces(monkeypatch):
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

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "project", "list"])

    assert result.exit_code == 0
    payload = json.loads(result.output)["data"]
    assert payload["total"] == 4
    assert calls == [WS_BAD, WS_GOOD, WS_CPU, WS_GPU]


def test_project_list_all_workspaces_bypasses_fanout_limit(monkeypatch):
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

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--json", "project", "list", "--all-workspaces"])

    assert result.exit_code == 0
    payload = json.loads(result.output)["data"]
    assert payload["total"] == 4
    assert calls == [WS_BAD, WS_GOOD, WS_CPU, WS_GPU]


def test_project_list_uses_cache_for_all_workspaces(monkeypatch, tmp_path):
    session_obj = FakeSession(
        all_workspace_ids=[WS_BAD, WS_GOOD, WS_CPU, WS_GPU],
        workspace_id=WS_GOOD,
    )
    monkeypatch.setattr(
        notebook_cli_module.web_session_module,
        "get_web_session",
        lambda: session_obj,
    )
    monkeypatch.setattr(project_cmd_module, "_PROJECT_LIST_CACHE_TTL_SECONDS", 600)
    monkeypatch.setattr(project_cmd_module, "_PROJECT_LIST_MAX_WORKERS", 1)

    calls: list[str | None] = []

    def fake_list_projects(workspace_id=None, session=None):  # type: ignore[no-untyped-def]
        calls.append(workspace_id)
        if workspace_id is None:
            return []
        return [_project(f"project-{workspace_id}", workspace_id, workspace_id)]

    monkeypatch.setattr(browser_api_module, "list_projects", fake_list_projects)

    runner = CliRunner()
    first = runner.invoke(cli_main, ["--json", "project", "list"])
    second = runner.invoke(cli_main, ["--json", "project", "list"])

    assert first.exit_code == 0
    assert second.exit_code == 0
    first_payload = json.loads(first.output)["data"]
    second_payload = json.loads(second.output)["data"]
    assert first_payload["total"] == 4
    assert second_payload["total"] == 4
    assert calls == [WS_BAD, WS_GOOD, WS_CPU, WS_GPU]
