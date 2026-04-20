"""Browser API wrappers for user-centric endpoints: quota, API keys, permissions.

The basic `user/detail` + `user/routes/{ws}` live in
[`platform.web.browser_api.jobs`](jobs.py) and [`workspaces.py`](workspaces.py).
This module covers the "userCenter" additions found via Playwright capture:
API-key management, per-workspace permission matrix, and user quota.
"""

from __future__ import annotations

from typing import Any, Optional

from inspire.platform.web.browser_api.core import (
    _browser_api_path,
    _get_base_url,
    _request_json,
)
from inspire.platform.web.session import DEFAULT_WORKSPACE_ID, WebSession, get_web_session

__all__ = [
    "get_user_quota",
    "list_user_api_keys",
    "get_user_permissions",
]


def _referer(path: str) -> str:
    return f"{_get_base_url()}{path}"


def get_user_quota(session: Optional[WebSession] = None) -> dict[str, Any]:
    """Return the current user's quota payload (GET /api/v1/user/quota)."""
    if session is None:
        session = get_web_session()
    data = _request_json(
        session,
        "GET",
        _browser_api_path("/user/quota"),
        referer=_referer("/userCenter"),
        timeout=15,
    )
    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")
    return data.get("data") or {}


def list_user_api_keys(session: Optional[WebSession] = None) -> list[dict[str, Any]]:
    """List the current user's API keys (GET /api/v1/user/my-api-key/list).

    Returns the raw `items` array; individual keys carry metadata like
    `id / name / create_at / last_used_at`. The key value itself is only
    available at create time.
    """
    if session is None:
        session = get_web_session()
    data = _request_json(
        session,
        "GET",
        _browser_api_path("/user/my-api-key/list"),
        referer=_referer("/userCenter"),
        timeout=15,
    )
    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")
    items = (data.get("data") or {}).get("items")
    return items if isinstance(items, list) else []


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
        # Legacy matrix shape: dict[permission -> bool]; keep only granted keys.
        return [k for k, v in perms.items() if v]
    return []
