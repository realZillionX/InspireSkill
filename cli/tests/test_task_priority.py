"""Focused tests for workspace-aware task-priority policy."""

from __future__ import annotations

import pytest

from inspire.platform.web.browser_api import workspaces
from inspire.platform.web.session.models import WebSession
from inspire.task_priority import (
    TaskPriorityError,
    is_low_task_priority,
    resolve_task_priority,
)

FAIR_WORKSPACE_ID = "ws-11111111-1111-1111-1111-111111111111"
STANDARD_WORKSPACE_ID = "ws-22222222-2222-2222-2222-222222222222"
MISSING_FLAG_WORKSPACE_ID = "ws-33333333-3333-3333-3333-333333333333"
NON_BOOL_WORKSPACE_ID = "ws-44444444-4444-4444-4444-444444444444"


def _session(**kwargs: object) -> WebSession:
    return WebSession(
        storage_state={"cookies": [], "origins": []},
        created_at=0.0,
        workspace_id=FAIR_WORKSPACE_ID,
        **kwargs,
    )


@pytest.mark.parametrize("requested", [1, 4])
def test_fair_workspace_accepts_only_exposed_priorities(requested: int) -> None:
    assert resolve_task_priority(requested, fair_scheduling=True) == requested


def test_fair_workspace_defaults_to_high_priority() -> None:
    assert resolve_task_priority(None, fair_scheduling=True) == 4


@pytest.mark.parametrize(
    ("project_limit", "expected"),
    [(1, 1), (2, 1), (3, 1), (4, 4), (5, 4), (10, 4)],
)
def test_fair_project_limit_is_normalized(
    project_limit: int,
    expected: int,
) -> None:
    assert (
        resolve_task_priority(
            4,
            fair_scheduling=True,
            project_limit=project_limit,
        )
        == expected
    )


def test_standard_workspace_keeps_full_range_and_default() -> None:
    assert resolve_task_priority(None, fair_scheduling=False) == 10
    assert [
        resolve_task_priority(priority, fair_scheduling=False) for priority in range(1, 11)
    ] == list(range(1, 11))


@pytest.mark.parametrize(
    ("requested", "fair_scheduling"),
    [
        (0, True),
        (2, True),
        (3, True),
        (5, True),
        (10, True),
        (0, False),
        (11, False),
        (True, True),
        (False, False),
        ("4", True),
        (4.0, False),
    ],
)
def test_invalid_requested_priority_is_rejected(
    requested: object,
    fair_scheduling: bool,
) -> None:
    with pytest.raises(TaskPriorityError):
        resolve_task_priority(requested, fair_scheduling=fair_scheduling)  # type: ignore[arg-type]


@pytest.mark.parametrize("priority", [1, 2, 3])
def test_historical_low_priorities_remain_low(priority: int) -> None:
    assert is_low_task_priority(priority) is True


@pytest.mark.parametrize("priority", [0, 4, 5, 10, None, "invalid"])
def test_non_low_priorities_are_not_classified_as_low(priority: object) -> None:
    assert is_low_task_priority(priority) is False


def test_workspace_capability_requires_an_exact_true_boolean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def _request_json(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {
            "data": {
                "routes": [
                    {
                        "name": "userWorkspaceList",
                        "routes": [
                            {
                                "path": FAIR_WORKSPACE_ID,
                                "name": "Fair",
                                "is_fair_workspace": True,
                            },
                            {
                                "path": STANDARD_WORKSPACE_ID,
                                "name": "Standard",
                                "is_fair_workspace": False,
                            },
                            {
                                "path": MISSING_FLAG_WORKSPACE_ID,
                                "name": "Missing flag",
                            },
                            {
                                "path": NON_BOOL_WORKSPACE_ID,
                                "name": "Non-bool flag",
                                "is_fair_workspace": 1,
                            },
                        ],
                    }
                ]
            }
        }

    monkeypatch.setattr(workspaces, "_request_json", _request_json)
    session = _session()

    result = workspaces.try_enumerate_workspaces(
        session,
        base_url="https://example.invalid",
    )

    capabilities = {item["id"]: item["is_fair_workspace"] for item in result}
    assert capabilities == {
        FAIR_WORKSPACE_ID: True,
        STANDARD_WORKSPACE_ID: False,
        MISSING_FLAG_WORKSPACE_ID: False,
        NON_BOOL_WORKSPACE_ID: False,
    }
    assert session.all_workspace_fair_scheduling == capabilities
    assert workspaces.is_fair_scheduling_workspace(session, FAIR_WORKSPACE_ID) is True
    assert workspaces.is_fair_scheduling_workspace(session, STANDARD_WORKSPACE_ID) is False
    assert calls == 1


def test_workspace_capability_failure_is_not_treated_as_standard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("route lookup failed")

    monkeypatch.setattr(workspaces, "_request_json", _fail)

    with pytest.raises(workspaces.WorkspaceCapabilityError):
        workspaces.is_fair_scheduling_workspace(
            _session(),
            STANDARD_WORKSPACE_ID,
        )


def test_workspace_capability_cache_avoids_route_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unexpected(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("cached capability should not issue a request")

    monkeypatch.setattr(workspaces, "_request_json", _unexpected)
    session = _session(all_workspace_fair_scheduling={FAIR_WORKSPACE_ID: True})

    assert workspaces.is_fair_scheduling_workspace(session, FAIR_WORKSPACE_ID) is True
