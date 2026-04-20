"""Unit tests for `inspire.platform.web.browser_api.servings`.

The Browser API serving endpoints have no public contract, so these tests
pin the wire-format parsing we reverse-engineered from the
`/jobs/modelDeployment` page: body shape (`filter_by: {my_serving: ...}`),
the list-or-`inference_servings` key fallback, `created_by` nested-object
flattening, and the `code != 0` error path. The live account used during
development had no servings in any of its 11 workspaces, so these unit
tests are the only coverage for the happy-path.
"""

from __future__ import annotations

from typing import Any

import pytest

from inspire.platform.web.browser_api import servings as servings_module
from inspire.platform.web.browser_api.servings import (
    ServingInfo,
    get_serving_configs,
    get_serving_detail,
    list_serving_user_project,
    list_servings,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeSession:
    """Session stand-in; the wrappers only read `.workspace_id`."""

    def __init__(self, workspace_id: str | None = "ws-default") -> None:
        self.workspace_id = workspace_id


def _install_fake_request(
    monkeypatch: pytest.MonkeyPatch, response: dict, record: dict
) -> None:
    """Monkey-patch the module-local `_request_json` to capture the outgoing call."""

    def _fake(session, method, url, *, referer=None, body=None, timeout=30, **kwargs):
        record["session"] = session
        record["method"] = method
        record["url"] = url
        record["referer"] = referer
        record["body"] = body
        record["timeout"] = timeout
        return response

    monkeypatch.setattr(servings_module, "_request_json", _fake)


# ---------------------------------------------------------------------------
# list_servings
# ---------------------------------------------------------------------------


def test_list_servings_posts_expected_body_and_parses_response(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "code": 0,
            "data": {
                "inference_servings": [
                    {
                        "inference_serving_id": "sv-abc",
                        "name": "demo-serving",
                        "status": "RUNNING",
                        "replicas": 2,
                        "image": "reg/img:latest",
                        "project_id": "project-1",
                        "workspace_id": "ws-override",
                        "logic_compute_group_id": "lcg-1",
                        "created_at": "1770000000000",
                        "created_by": {"id": "user-1", "name": "Alice"},
                    }
                ],
                "total": 7,
            },
        },
        record,
    )

    items, total = list_servings(
        workspace_id="ws-given",
        my_serving=True,
        page=2,
        page_size=20,
        session=_FakeSession(workspace_id="ws-session-default"),
    )

    assert total == 7
    assert len(items) == 1
    item = items[0]
    assert isinstance(item, ServingInfo)
    assert item.inference_serving_id == "sv-abc"
    assert item.name == "demo-serving"
    assert item.status == "RUNNING"
    assert item.replicas == 2
    assert item.created_by == "Alice"  # nested object flattened to display name

    # Wire-format: POST, correct endpoint, correct body.
    assert record["method"] == "POST"
    assert record["url"].endswith("/inference_servings/list")
    assert record["body"] == {
        "page": 2,
        "page_size": 20,
        "filter_by": {"my_serving": True},
        "workspace_id": "ws-given",
    }


def test_list_servings_resolves_workspace_from_session_when_not_passed(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch, {"code": 0, "data": {"inference_servings": [], "total": 0}}, record
    )

    list_servings(session=_FakeSession(workspace_id="ws-session"))
    assert record["body"]["workspace_id"] == "ws-session"
    # Default `my_serving` should be True (matches UI "我的部署").
    assert record["body"]["filter_by"] == {"my_serving": True}


def test_list_servings_falls_back_to_list_key_when_inference_servings_missing(
    monkeypatch,
) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"code": 0, "data": {"list": [{"id": "sv-1", "name": "x"}], "total": 1}},
        record,
    )
    items, total = list_servings(session=_FakeSession())
    assert total == 1
    assert items[0].inference_serving_id == "sv-1"  # falls back from `id`


def test_list_servings_raises_on_nonzero_code(monkeypatch) -> None:
    _install_fake_request(monkeypatch, {"code": 1234, "message": "bad"}, {})
    with pytest.raises(ValueError, match="API error: bad"):
        list_servings(session=_FakeSession())


def test_list_servings_empty_response_returns_empty_list_and_zero_total(monkeypatch) -> None:
    _install_fake_request(monkeypatch, {"code": 0, "data": None}, {})
    items, total = list_servings(session=_FakeSession())
    assert items == []
    assert total == 0


# ---------------------------------------------------------------------------
# get_serving_configs / list_serving_user_project / get_serving_detail
# ---------------------------------------------------------------------------


def test_get_serving_configs_uses_get_and_workspace_path(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"code": 0, "data": {"enable_auto_stop": False, "items": []}},
        record,
    )
    data = get_serving_configs(workspace_id="ws-abc", session=_FakeSession())
    assert data == {"enable_auto_stop": False, "items": []}
    assert record["method"] == "GET"
    assert record["url"].endswith("/inference_servings/configs/workspace/ws-abc")


def test_list_serving_user_project_posts_workspace_id(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"code": 0, "data": {"projects": [{"id": "p1"}], "users": []}},
        record,
    )
    data = list_serving_user_project(
        workspace_id="ws-xx", session=_FakeSession()
    )
    assert data == {"projects": [{"id": "p1"}], "users": []}
    assert record["method"] == "POST"
    assert record["url"].endswith("/inference_servings/user_project/list")
    assert record["body"] == {"workspace_id": "ws-xx"}


def test_get_serving_detail_uses_query_param(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch, {"code": 0, "data": {"status": "RUNNING"}}, record
    )
    data = get_serving_detail("sv-xyz", session=_FakeSession())
    assert data == {"status": "RUNNING"}
    assert record["method"] == "GET"
    assert "inference_serving_id=sv-xyz" in record["url"]


def test_get_serving_detail_raises_on_error(monkeypatch) -> None:
    _install_fake_request(monkeypatch, {"code": 404, "message": "not found"}, {})
    with pytest.raises(ValueError, match="API error: not found"):
        get_serving_detail("sv-missing", session=_FakeSession())
