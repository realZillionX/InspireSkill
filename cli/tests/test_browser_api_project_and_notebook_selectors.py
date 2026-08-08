"""Browser API tests for selector-style project and notebook endpoints."""

from __future__ import annotations

from typing import Any

import pytest

from inspire.platform.web.browser_api import notebooks as notebooks_module
from inspire.platform.web.browser_api import projects as projects_module
from inspire.platform.web.browser_api.notebooks import (
    list_notebook_lifecycle,
    list_notebook_users,
)
from inspire.platform.web.browser_api.projects import (
    list_all_projects,
    list_project_page_records,
    list_project_selector_records,
)


class _FakeSession:
    def __init__(self, workspace_id: str | None = "ws-default") -> None:
        self.workspace_id = workspace_id


def _install_fake_request(module, monkeypatch: pytest.MonkeyPatch, response: dict, record):
    def _fake(session, method, url, *, referer=None, body=None, timeout=30, **kwargs):
        record["session"] = session
        record["method"] = method
        record["url"] = url
        record["referer"] = referer
        record["body"] = body
        record["timeout"] = timeout
        return response

    monkeypatch.setattr(module, "_request_json", _fake)


def test_list_project_selector_records_posts_frontend_selector_body(monkeypatch: pytest.MonkeyPatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        projects_module,
        monkeypatch,
        {
            "ResponseMetadata": {},
            "Result": {"items": [{"id": "project-1", "name": "Demo"}], "total": "1"},
        },
        record,
    )

    items, total = list_project_selector_records(workspace_id="ws-x", session=_FakeSession())

    assert total == 1
    assert items[0]["name"] == "Demo"
    assert record["method"] == "POST"
    assert record["url"] == "/api/v2/project?Action=ListProjects"
    assert record["body"] == {
        "filter": {"workspace_id": "ws-x", "check_admin": True},
        "page": 1,
        "page_size": -1,
    }


def test_list_project_selector_records_can_omit_check_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        projects_module,
        monkeypatch,
        {"ResponseMetadata": {}, "Result": {"items": [], "total": 0}},
        record,
    )

    list_project_selector_records(workspace_id="ws-x", check_admin=None, session=_FakeSession())

    assert record["body"]["filter"] == {"workspace_id": "ws-x"}


def test_list_project_page_records_posts_management_body(monkeypatch: pytest.MonkeyPatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        projects_module,
        monkeypatch,
        {"Result": {"items": [{"id": "project-1"}], "total": 3}},
        record,
    )

    items, total = list_project_page_records(
        page=2,
        page_size=5,
        filter_body={"name": "demo"},
        session=_FakeSession(),
    )

    assert total == 3
    assert len(items) == 1
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/project?Action=GetProjectForPage")
    assert record["body"] == {"page": 2, "page_size": 5, "filter": {"name": "demo"}}


def test_list_all_projects_posts_single_unscoped_project_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        projects_module,
        monkeypatch,
        {
            "ResponseMetadata": {},
            "Result": {
                "items": [
                    {
                        "id": "project-1",
                        "name": "Demo",
                        "member_remain_budget": "12.5",
                        "space_list": [{"id": "ws-1", "name": "Workspace One"}],
                    }
                ],
                "total": 1,
            },
        },
        record,
    )

    projects = list_all_projects(session=_FakeSession())

    assert len(projects) == 1
    assert projects[0].project_id == "project-1"
    assert projects[0].member_remain_budget == 12.5
    assert projects[0].workspace_id == "ws-1"
    assert projects[0].workspace_ids == ("ws-1",)
    assert projects[0].workspace_names == ("Workspace One",)
    assert record["method"] == "POST"
    assert record["url"] == "/api/v2/project?Action=ListProjects"
    assert record["body"] == {"page": 1, "page_size": 100, "filter": {"check_admin": True}}


def test_list_all_projects_paginates_project_query(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def _fake(session, method, url, *, referer=None, body=None, timeout=30, **kwargs):
        calls.append(body)
        page = body["page"]
        if page == 1:
            return {
                "ResponseMetadata": {},
                "Result": {
                    "items": [{"id": f"project-{idx}", "name": f"Demo {idx}"} for idx in range(100)],
                    "total": 101,
                },
            }
        return {
            "ResponseMetadata": {},
            "Result": {"items": [{"id": "project-100", "name": "Demo 100"}], "total": 101},
        }

    monkeypatch.setattr(projects_module, "_request_json", _fake)

    projects = list_all_projects(session=_FakeSession())

    assert len(projects) == 101
    assert [call["page"] for call in calls] == [1, 2]
    assert all(call["page_size"] == 100 for call in calls)


def test_list_notebook_users_posts_workspace_id(monkeypatch: pytest.MonkeyPatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        notebooks_module,
        monkeypatch,
        {
            "Result": {
                "list": [{"id": "user-1", "name": "Alice"}],
                "total": "1",
            },
        },
        record,
    )

    users, total = list_notebook_users(session=_FakeSession(workspace_id="ws-session"))

    assert total == 1
    assert users[0]["name"] == "Alice"
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/notebook?Action=ListNotebookCreators")
    assert record["body"] == {"workspace_id": "ws-session"}


def test_list_notebook_lifecycle_omits_empty_time_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        notebooks_module,
        monkeypatch,
        {"Result": {"list": [], "total": 0}},
        record,
    )

    list_notebook_lifecycle("notebook-1", session=_FakeSession())

    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/notebook?Action=ListNotebookLifecycles")
    assert record["body"] == {
        "notebook_id": "notebook-1",
        "page": 1,
        "page_size": 200,
    }
