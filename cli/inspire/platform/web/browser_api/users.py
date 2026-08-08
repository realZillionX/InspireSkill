"""Browser API wrappers for user-centric endpoints.

The basic `user/detail` + `user/routes/{ws}` live in
[`platform.web.browser_api.jobs`](jobs.py) and [`workspaces.py`](workspaces.py).
This module covers the "userCenter" additions found via Playwright capture:
API-key metadata, SSH key management, and the per-workspace permission matrix.
"""

from __future__ import annotations

from typing import Any, Optional

from inspire.platform.web.browser_api.core import (
    _browser_api_path,
    _get_base_url,
    _request_json,
    _v2_result,
)
from inspire.platform.web.session import DEFAULT_WORKSPACE_ID, WebSession, get_web_session

__all__ = [
    "create_user_ssh_key",
    "delete_user_ssh_key",
    "get_user_detail",
    "list_user_api_keys",
    "list_user_ssh_keys",
    "get_user_permissions",
]


def _referer(path: str) -> str:
    return f"{_get_base_url()}{path}"


def get_user_detail(
    user_id: str,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Return a user detail payload by user id (GET /api/v1/user/{user_id})."""
    if session is None:
        session = get_web_session()
    raw_id = user_id.strip()
    if not raw_id:
        raise ValueError("User selection is required.")
    data = _request_json(
        session,
        "GET",
        _browser_api_path(f"/user/{raw_id}"),
        referer=_referer("/userCenter"),
        timeout=15,
    )
    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")
    payload = data.get("data")
    return payload if isinstance(payload, dict) else {}


def list_user_api_keys(session: Optional[WebSession] = None) -> list[dict[str, Any]]:
    """List the current user's API keys via ``user.ListAPIKeys``.

    Returns the raw `items` array; individual keys carry metadata like
    `id / name / create_at / last_used_at`. The key value itself is only
    available at create time.
    """
    if session is None:
        session = get_web_session()
    payload = _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/user?Action=ListAPIKeys",
            referer=_referer("/userCenter"),
            body={},
            timeout=15,
        )
    )
    items = payload.get("items")
    return items if isinstance(items, list) else []


def list_user_ssh_keys(
    *,
    page: int = 1,
    page_size: int = 100,
    session: Optional[WebSession] = None,
) -> tuple[list[dict[str, Any]], int]:
    """List the current user's SSH public keys (POST /api/v1/ssh/list)."""
    if session is None:
        session = get_web_session()
    data = _request_json(
        session,
        "POST",
        _browser_api_path("/ssh/list"),
        referer=_referer("/userCenter?tab=sshkey"),
        body={"page": page, "page_size": page_size},
        timeout=15,
    )
    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")
    payload = data.get("data") or {}
    items = payload.get("list")
    if not isinstance(items, list):
        items = payload.get("items")
    if not isinstance(items, list):
        items = []
    total = payload.get("total")
    if total is None:
        total_int = len(items)
    else:
        try:
            total_int = int(str(total))
        except ValueError:
            total_int = len(items)
    return [item for item in items if isinstance(item, dict)], total_int


def create_user_ssh_key(
    *,
    name: str,
    content: str,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Create an SSH public key (POST /api/v1/ssh/create).

    The frontend form field is `content`; `public_key`, `key`, and `ssh_key`
    are rejected by the backend proto schema.
    """
    if session is None:
        session = get_web_session()
    key_name = name.strip()
    key_content = content.strip()
    if not key_name:
        raise ValueError("SSH key name is required")
    if not key_content:
        raise ValueError("SSH public key content is required")
    data = _request_json(
        session,
        "POST",
        _browser_api_path("/ssh/create"),
        referer=_referer("/userCenter?tab=sshkey"),
        body={"name": key_name, "content": key_content},
        timeout=15,
    )
    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")
    payload = data.get("data")
    return payload if isinstance(payload, dict) else {}


def delete_user_ssh_key(
    ssh_id: str,
    *,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Delete an SSH public key (DELETE /api/v1/ssh/{ssh_id})."""
    if session is None:
        session = get_web_session()
    raw_id = ssh_id.strip()
    if not raw_id:
        raise ValueError("SSH key selection is required.")
    data = _request_json(
        session,
        "DELETE",
        _browser_api_path(f"/ssh/{raw_id}"),
        referer=_referer("/userCenter?tab=sshkey"),
        timeout=15,
    )
    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")
    payload = data.get("data")
    return payload if isinstance(payload, dict) else {}


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
