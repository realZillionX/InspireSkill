"""Browser API wrapper for the per-workspace permission matrix.

Backs `inspire account permissions`. The current user's identity comes from
[`jobs.get_current_user`](jobs.py) and workspace routes from
[`workspaces.py`](workspaces.py); this module only covers `user.GetPermissions`.
"""

from __future__ import annotations

from typing import Optional

from inspire.platform.web.browser_api.core import (
    _get_base_url,
    _request_json,
    _v2_result,
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
    """Fetch granted permissions for a workspace (`user.GetPermissions`).

    Returns a flat list of permission codes (e.g. `"job.trainingJob.create"`).

    `GetPermissions` is absent from discovery but live, and answers the v1
    `/user/permissions/{workspace_id}` payload verbatim. The workspace moves
    from the URL into a `WorkspaceId` body field.
    """
    if session is None:
        session = get_web_session()
    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID
    payload = _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/user?Action=GetPermissions",
            referer=_referer("/"),
            body={"WorkspaceId": workspace_id},
            timeout=15,
        )
    )
    perms = payload.get("permissions")
    if isinstance(perms, list):
        return [str(p) for p in perms]
    if isinstance(perms, dict):
        # Some responses use a permission-to-granted mapping.
        return [k for k, v in perms.items() if v]
    return []
