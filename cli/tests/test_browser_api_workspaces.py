from __future__ import annotations

import pytest

from inspire.platform.web.browser_api import workspaces
from inspire.platform.web.session.models import DEFAULT_WORKSPACE_ID, WebSession
from inspire.platform.web.session.workspaces import parse_workspace_catalog

WORKSPACE_ID = "ws-11111111-1111-1111-1111-111111111111"
STANDARD_ID = "ws-22222222-2222-2222-2222-222222222222"
ITEM = {"workspace_id": WORKSPACE_ID, "name": "Training", "is_fair_workspace": True}


@pytest.mark.parametrize("envelope", ["Result", "data"])
def test_workspace_catalog_preserves_names_and_capabilities(envelope):
    ids, names, fair = parse_workspace_catalog({envelope: {"items": [
        ITEM,
        {"workspace_id": STANDARD_ID, "name": "CPU"},
    ]}})

    assert ids == [WORKSPACE_ID, STANDARD_ID]
    assert names == {WORKSPACE_ID: "Training", STANDARD_ID: "CPU"}
    assert fair == {WORKSPACE_ID: True, STANDARD_ID: False}


def test_workspace_catalog_accepts_empty_list():
    assert parse_workspace_catalog({"Result": {"items": []}}) == ([], {}, {})


@pytest.mark.parametrize("result", [
    {},
    {"routes": [{"name": "jobs"}]},
    {"items": None},
    {"items": {}},
    {"items": [ITEM, None]},
    {"items": [{**ITEM, "workspace_id": "default"}]},
    {"items": [{**ITEM, "workspace_id": DEFAULT_WORKSPACE_ID}]},
    {"items": [{**ITEM, "workspace_id": 1}]},
    {"items": [{**ITEM, "name": ""}]},
    {"items": [{**ITEM, "name": None}]},
    {"items": [{**ITEM, "is_fair_workspace": 1}]},
])
def test_workspace_catalog_rejects_malformed_results(result):
    with pytest.raises(ValueError):
        parse_workspace_catalog({"Result": result})


@pytest.mark.parametrize("payload", [
    {"ResponseMetadata": {"Error": {"Code": "AccessForbidden", "Message": "denied"}}},
    {"code": 403, "message": "denied", "data": {}},
])
def test_workspace_catalog_propagates_api_errors(payload):
    with pytest.raises(ValueError):
        parse_workspace_catalog(payload)


@pytest.mark.parametrize("current_workspace", [None, DEFAULT_WORKSPACE_ID, STANDARD_ID])
def test_workspace_enumeration_is_unscoped(monkeypatch, current_workspace):
    session = WebSession(storage_state={}, created_at=1, workspace_id=current_workspace)
    session.all_workspace_fair_scheduling = {STANDARD_ID: True}
    calls = []

    def request(supplied_session, method, path, **kwargs):
        calls.append((method, path, kwargs["body"]))
        assert supplied_session is session
        assert kwargs["referer"] == "https://example.test/jobs/spacesOverview"
        return {"Result": {"items": [ITEM]}}

    monkeypatch.setattr(workspaces, "_request_json", request)
    rows = workspaces.try_enumerate_workspaces(session, base_url="https://example.test")

    assert calls == [("POST", "/api/v2/user?Action=ListAccessibleWorkspaces", {})]
    assert rows == [{"id": WORKSPACE_ID, "name": "Training", "is_fair_workspace": True}]
    assert session.all_workspace_fair_scheduling == {WORKSPACE_ID: True}


@pytest.mark.parametrize("payload", [
    {"Result": {"routes": []}},
    {"ResponseMetadata": {"Error": {"Code": "AccessForbidden"}}},
])
def test_workspace_enumeration_failure_preserves_capabilities(monkeypatch, payload):
    session = WebSession(
        storage_state={}, created_at=1, all_workspace_fair_scheduling={WORKSPACE_ID: True},
    )
    monkeypatch.setattr(workspaces, "_request_json", lambda *a, **kw: payload)

    with pytest.raises(workspaces.WorkspaceEnumerationError) as caught:
        workspaces.try_enumerate_workspaces(session, base_url="https://example.test")

    assert isinstance(caught.value.__cause__, ValueError)
    assert session.all_workspace_fair_scheduling == {WORKSPACE_ID: True}


def test_empty_workspace_enumeration_clears_capabilities(monkeypatch):
    session = WebSession(
        storage_state={}, created_at=1, all_workspace_fair_scheduling={WORKSPACE_ID: True},
    )
    monkeypatch.setattr(
        workspaces, "_request_json", lambda *a, **kw: {"Result": {"items": []}},
    )

    assert workspaces.try_enumerate_workspaces(session, base_url="https://example.test") == []
    assert session.all_workspace_fair_scheduling == {}
