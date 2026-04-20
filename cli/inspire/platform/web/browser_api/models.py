"""Browser API wrappers for the model registry.

Reverse-engineered from `/jobs/modelDeployment` and `/modelLibrary`.
No OpenAPI counterpart — this is Browser-API-only. See
[cli/scripts/reverse_capture/](../../../../scripts/reverse_capture/) for the
capture methodology.

Wire-format notes:
- `POST /api/v1/model/list` body `{page, page_size, filter_by:{}, workspace_id}`.
- `POST /api/v1/model/detail` body `{model_id}` → `{model, project_name, user_avatar, user_name}`.
- `GET /api/v1/model/{model_id}` and `GET /api/v1/model/{model_id}/versions` both
  return the same `{list, next_version, total}` shape — the path with `/versions`
  is the canonical one used by the UI "版本列表" drawer.
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
    "ModelInfo",
    "get_model_detail",
    "list_model_versions",
    "list_models",
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
class ModelInfo:
    model_id: str
    name: str
    id: str = ""  # numeric internal id
    description: str = ""
    has_published: bool = False
    is_vllm_compatible: bool = False
    created_at: str = ""
    latest_version: str = ""
    raw: dict[str, Any] | None = None


def _parse_model(item: dict[str, Any]) -> ModelInfo:
    """Flatten the `/model/list` item shape (`{model: {...}, ...}`) into `ModelInfo`."""
    if not isinstance(item, dict):
        return ModelInfo(model_id="", name="")
    inner = item.get("model") if isinstance(item.get("model"), dict) else item
    return ModelInfo(
        model_id=str(inner.get("model_id") or inner.get("id") or ""),
        name=str(inner.get("name") or inner.get("model_name") or ""),
        id=str(inner.get("id") or ""),
        description=str(inner.get("description") or ""),
        has_published=bool(inner.get("has_published", False)),
        is_vllm_compatible=bool(inner.get("is_vllm_compatible", False)),
        created_at=str(inner.get("created_at") or ""),
        latest_version=str(item.get("latest_version") or item.get("next_version") or ""),
        raw=item,
    )


def list_models(
    workspace_id: Optional[str] = None,
    *,
    page: int = 1,
    page_size: int = -1,
    filter_by: Optional[dict[str, Any]] = None,
    session: Optional[WebSession] = None,
) -> tuple[list[ModelInfo], int]:
    """List models via `POST /api/v1/model/list`.

    Returns `(items, total)`. `page_size=-1` mirrors the UI (fetch all).
    """
    session, workspace_id = _resolve_workspace(workspace_id, session)
    body = {
        "page": page,
        "page_size": page_size,
        "filter_by": filter_by or {},
        "workspace_id": workspace_id,
    }
    data = _request_json(
        session,
        "POST",
        _browser_api_path("/model/list"),
        referer=_referer(),
        body=body,
        timeout=30,
    )
    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    payload = data.get("data") or {}
    raw_items = payload.get("list") or []
    total = int(payload.get("total") or len(raw_items) or 0)
    return [_parse_model(it) for it in raw_items if isinstance(it, dict)], total


def get_model_detail(
    model_id: str,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Get model detail via `POST /api/v1/model/detail`.

    Returns the raw `data` dict — typically
    `{model: {...}, project_name, user_avatar, user_name}`.
    """
    if session is None:
        session = get_web_session()
    data = _request_json(
        session,
        "POST",
        _browser_api_path("/model/detail"),
        referer=_referer(),
        body={"model_id": model_id},
        timeout=30,
    )
    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")
    return data.get("data") or {}


def list_model_versions(
    model_id: str,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """List versions of a model via `GET /api/v1/model/{model_id}/versions`.

    Returns the raw `data` dict (`{list: [...], next_version, total}`).
    """
    if session is None:
        session = get_web_session()
    data = _request_json(
        session,
        "GET",
        _browser_api_path(f"/model/{model_id}/versions"),
        referer=_referer(),
        timeout=30,
    )
    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")
    return data.get("data") or {}
