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
