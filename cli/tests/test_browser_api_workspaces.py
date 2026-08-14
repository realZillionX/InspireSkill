from __future__ import annotations

import pytest

from inspire.platform.web.browser_api import workspaces


def test_workspace_enumeration_rejects_nonzero_api_code(monkeypatch) -> None:
    monkeypatch.setattr(
        workspaces,
        "_request_json",
        lambda *_args, **_kwargs: {
            "code": 403,
            "message": "permission denied",
            "data": {},
        },
    )

    with pytest.raises(workspaces.WorkspaceEnumerationError):
        workspaces.try_enumerate_workspaces(
            object(),  # type: ignore[arg-type]
            workspace_id="ws-12345678-1234-1234-1234-123456789abc",
        )


def test_workspace_enumeration_returns_empty_for_successful_empty_api(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        workspaces,
        "_request_json",
        lambda *_args, **_kwargs: {
            "code": 0,
            "data": {"routes": []},
        },
    )

    assert workspaces.try_enumerate_workspaces(
        object(),  # type: ignore[arg-type]
        workspace_id="ws-12345678-1234-1234-1234-123456789abc",
    ) == []


# ---------------------------------------------------------------------------
# Workspace quota
# ---------------------------------------------------------------------------


def _install_quota_responses(
    monkeypatch: pytest.MonkeyPatch,
    *,
    quota: dict,
    compute: dict,
    calls: list[dict],
):
    def _fake(session, method, url, *, referer=None, body=None, timeout=30, **kwargs):
        calls.append({"url": url, "body": body})
        if "GetWorkspaceQuota" in url:
            return {"Result": quota}
        return {"Result": compute}

    monkeypatch.setattr(workspaces, "_request_json", _fake)


_QUOTA = {
    "gpu_high_running": 10000,
    "gpu_high_running_used": 4682,
    "gpu_low_running": 20000,
    "gpu_low_running_used": 770,
    "cpu_high_running": -1,
    "cpu_high_running_used": 93583,
    "cpu_low_running": -1,
    "cpu_low_running_used": 14269,
    "memory_high_running": -1,
    "memory_high_running_used": 1045550,
    "memory_low_running": -1,
    "memory_low_running_used": 156428,
}
# The platform spells the key `logic_resouces`; that typo is the wire format.
_COMPUTE = {
    "logic_resouces": {
        "gpu_total": 5597,
        "gpu_used": 5452,
        "cpu_total": 126607,
        "cpu_used": 107852,
        "memory_gi_total": 1323666.07,
        "memory_gi_used": 1201978,
    }
}


def test_workspace_quota_uses_top_level_workspace_id(monkeypatch) -> None:
    # The nested `filter` envelope other `workspace.*` Actions want is rejected
    # here with unknown field "filter".
    calls: list[dict] = []
    _install_quota_responses(monkeypatch, quota=_QUOTA, compute=_COMPUTE, calls=calls)

    workspaces.get_workspace_quota_usage("ws-1", session=object())  # type: ignore[arg-type]

    assert [call["body"] for call in calls] == [
        {"workspace_id": "ws-1"},
        {"workspace_id": "ws-1"},
    ]
    assert calls[0]["url"].endswith("/api/v2/workspace?Action=GetWorkspaceQuota")
    assert calls[1]["url"].endswith(
        "/api/v2/workspace?Action=GetWorkspaceComputeResource"
    )


def test_workspace_quota_reports_limit_usage_and_capacity(monkeypatch) -> None:
    calls: list[dict] = []
    _install_quota_responses(monkeypatch, quota=_QUOTA, compute=_COMPUTE, calls=calls)

    rows = {
        row.resource: row
        for row in workspaces.get_workspace_quota_usage("ws-1", session=object())  # type: ignore[arg-type]
    }

    assert [row for row in rows] == ["gpu", "cpu", "memory_gib"]
    assert rows["gpu"].limit == 10000
    assert rows["gpu"].used == 4682
    assert rows["gpu"].available == 5318
    assert rows["gpu"].unlimited is False
    assert rows["gpu"].capacity == 5597
    assert rows["gpu"].capacity_used == 5452


def test_workspace_quota_marks_minus_one_as_unlimited(monkeypatch) -> None:
    calls: list[dict] = []
    _install_quota_responses(monkeypatch, quota=_QUOTA, compute=_COMPUTE, calls=calls)

    rows = {
        row.resource: row
        for row in workspaces.get_workspace_quota_usage("ws-1", session=object())  # type: ignore[arg-type]
    }

    assert rows["cpu"].limit == workspaces.UNLIMITED_QUOTA
    assert rows["cpu"].unlimited is True
    # An unlimited ceiling has no meaningful remainder to report.
    assert rows["cpu"].available is None
    assert rows["cpu"].used == 93583


def test_workspace_quota_low_priority_is_a_separate_allowance(monkeypatch) -> None:
    # High and low priority draw against different ceilings, so reading one and
    # reporting it as the other would misstate both.
    calls: list[dict] = []
    _install_quota_responses(monkeypatch, quota=_QUOTA, compute=_COMPUTE, calls=calls)

    rows = {
        row.resource: row
        for row in workspaces.get_workspace_quota_usage(
            "ws-1",
            session=object(),  # type: ignore[arg-type]
            priority="low",
        )
    }

    assert rows["gpu"].limit == 20000
    assert rows["gpu"].used == 770


def test_workspace_quota_tolerates_a_missing_compute_summary(monkeypatch) -> None:
    calls: list[dict] = []
    _install_quota_responses(monkeypatch, quota=_QUOTA, compute={}, calls=calls)

    rows = workspaces.get_workspace_quota_usage("ws-1", session=object())  # type: ignore[arg-type]

    assert all(row.capacity is None for row in rows)
    assert rows[0].used == 4682
