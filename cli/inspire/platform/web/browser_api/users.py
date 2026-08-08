"""Browser API wrapper for the per-workspace permission matrix.

Backs `inspire account permissions`. The current user's identity comes from
[`jobs.get_current_user`](jobs.py) and workspace routes from
[`workspaces.py`](workspaces.py); this module only covers
`/user/permissions/{workspace_id}`, which has no v2 counterpart.
"""

from __future__ import annotations

from typing import Optional

from inspire.platform.web.browser_api.core import (
    _browser_api_path,
    _get_base_url,
    _request_json,
)
from inspire.platform.web.session import DEFAULT_WORKSPACE_ID, WebSession, get_web_session

__all__ = [
    "get_user_permissions",
]


def _referer(path: str) -> str:
    return f"{_get_base_url()}{path}"


def get_user_permissions(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[str]:
    """Fetch granted permissions for a workspace (GET /api/v1/user/permissions/{ws}).

    Returns a flat list of permission codes (e.g. `"job.trainingJob.create"`).
    """
    if session is None:
        session = get_web_session()
    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID
    data = _request_json(
        session,
        "GET",
        _browser_api_path(f"/user/permissions/{workspace_id}"),
        referer=_referer("/"),
        timeout=15,
    )
    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")
    perms = (data.get("data") or {}).get("permissions")
    if isinstance(perms, list):
        return [str(p) for p in perms]
    if isinstance(perms, dict):
        # Some responses use a permission-to-granted mapping.
        return [k for k, v in perms.items() if v]
    return []
