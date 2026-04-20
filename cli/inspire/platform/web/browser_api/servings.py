"""Browser API wrappers for inference servings (model deployment).

OpenAPI covers `create / detail / stop` (see `platform.openapi.inference_servings`).
Browser API fills in everything the UI needs on the `/jobs/modelDeployment` page:
listing, configs per workspace, and the user+project pickers for the create
dialog. Reverse-engineered via Playwright — see
[cli/scripts/reverse_capture/](../../../../scripts/reverse_capture/).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from inspire.platform.web.browser_api.core import (
    _browser_api_path,
    _get_base_url,
    _request_json,
)
from inspire.platform.web.session import DEFAULT_WORKSPACE_ID, WebSession, get_web_session

__all__ = [
    "ServingInfo",
    "list_servings",
    "list_serving_user_project",
    "get_serving_configs",
    "get_serving_detail",
]


_REFERER_PATH = "/jobs/modelDeployment"


def _referer() -> str:
    return f"{_get_base_url()}{_REFERER_PATH}"


def _resolve_workspace(
    workspace_id: Optional[str], session: Optional[WebSession]
) -> tuple[WebSession, str]:
    if session is None:
        session = get_web_session()
    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID
    return session, workspace_id


@dataclass
class ServingInfo:
    inference_serving_id: str
    name: str
    status: str = ""
    replicas: int = 0
    image: str = ""
    project_id: str = ""
    workspace_id: str = ""
    logic_compute_group_id: str = ""
    created_at: str = ""
    created_by: str = ""
    raw: dict[str, Any] | None = None


def list_servings(
    workspace_id: Optional[str] = None,
    *,
    my_serving: bool = True,
    page: int = 1,
    page_size: int = 20,
    session: Optional[WebSession] = None,
) -> tuple[list[ServingInfo], int]:
    """List inference servings via `POST /api/v1/inference_servings/list`.

    Returns `(items, total)`. `my_serving=True` mirrors the UI's "我的部署"
    default; pass `False` to see the workspace-wide "全部部署" view.
    """
    session, workspace_id = _resolve_workspace(workspace_id, session)
    body = {
        "page": page,
        "page_size": page_size,
        "filter_by": {"my_serving": my_serving},
        "workspace_id": workspace_id,
    }
    data = _request_json(
        session,
        "POST",
        _browser_api_path("/inference_servings/list"),
        referer=_referer(),
        body=body,
        timeout=30,
    )
    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    payload = data.get("data") or {}
    raw_items = payload.get("inference_servings") or payload.get("list") or []
    total = int(payload.get("total") or len(raw_items) or 0)

    def _pick(item: dict, *keys: str, default: str = "") -> str:
        for k in keys:
            v = item.get(k)
            if v is not None and v != "":
                return str(v)
        return default

    def _created_by(item: dict) -> str:
        cb = item.get("created_by")
        if isinstance(cb, dict):
            return cb.get("name") or cb.get("id") or ""
        return str(cb or "")

    return (
        [
            ServingInfo(
                inference_serving_id=_pick(it, "inference_serving_id", "id"),
                name=_pick(it, "name"),
                status=_pick(it, "status", "phase"),
                replicas=int(it.get("replicas") or 0),
                image=_pick(it, "image"),
                project_id=_pick(it, "project_id"),
                workspace_id=_pick(it, "workspace_id", default=workspace_id),
                logic_compute_group_id=_pick(it, "logic_compute_group_id"),
                created_at=_pick(it, "created_at"),
                created_by=_created_by(it),
                raw=it if isinstance(it, dict) else None,
            )
            for it in raw_items
            if isinstance(it, dict)
        ],
        total,
    )


def list_serving_user_project(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Available projects + users for the create-serving dialog.

    Returns the raw `data` dict (`{projects: [...], users: [...]}`). The shape
    mirrors the UI drop-downs so we don't collapse it into typed objects here.
    """
    session, workspace_id = _resolve_workspace(workspace_id, session)
    data = _request_json(
        session,
        "POST",
        _browser_api_path("/inference_servings/user_project/list"),
        referer=_referer(),
        body={"workspace_id": workspace_id},
        timeout=30,
    )
    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")
    return data.get("data") or {}


def get_serving_configs(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Serving-time configs for a workspace (image / quota presets).

    Calls `GET /api/v1/inference_servings/configs/workspace/{workspace_id}`.
    Returns the raw `data` dict, typically `{configs: [...]}`.
    """
    session, workspace_id = _resolve_workspace(workspace_id, session)
    data = _request_json(
        session,
        "GET",
        _browser_api_path(f"/inference_servings/configs/workspace/{workspace_id}"),
        referer=_referer(),
        timeout=30,
    )
    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")
    return data.get("data") or {}


def get_serving_detail(
    inference_serving_id: str,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Browser API variant of serving detail.

    Calls `GET /api/v1/inference_servings/detail?inference_serving_id=<id>`.
    Prefer the OpenAPI form (`platform.openapi.inference_servings.get_inference_serving_detail`)
    when a Bearer token is available; use this when only the SSO cookie is.
    """
    if session is None:
        session = get_web_session()
    data = _request_json(
        session,
        "GET",
        _browser_api_path(
            f"/inference_servings/detail?inference_serving_id={inference_serving_id}"
        ),
        referer=_referer(),
        timeout=30,
    )
    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")
    return data.get("data") or {}
