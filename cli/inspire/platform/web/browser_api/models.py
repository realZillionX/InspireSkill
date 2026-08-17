"""Browser API wrappers for the model registry.

Reverse-engineered from the current `/jobs/modelService` page. Model registry
browsing and registration use the web-session Browser API; the route is
`/api/v2/model-hub` (hyphenated -- the underscore spelling 404s). The Action
contract and the controlled-verification discipline behind it are documented in
`references/dev/browser-api-actions.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from inspire.platform.web.browser_api.core import (
    _get_base_url,
    _request_json,
    _v2_result,
)
from inspire.platform.web.session import WebSession, get_web_session

__all__ = [
    "ModelInfo",
    "check_model_inference_serving_pending",
    "check_model_vllm_compatible",
    "create_model",
    "delete_model",
    "get_model_detail",
    "get_model_publish_prefill",
    "get_model_publish_status",
    "get_model_recommended_config",
    "get_model_vllm_compatibility",
    "list_model_inference_servings",
    "list_model_users",
    "list_model_version_records",
    "list_model_versions",
    "list_models",
]


_REFERER_PATH = "/jobs/modelService"


def _referer(workspace_id: str | None = None) -> str:
    url = f"{_get_base_url()}{_REFERER_PATH}"
    if workspace_id:
        return f"{url}?spaceId={workspace_id}"
    return url


def _resolve_workspace(
    workspace_id: Optional[str], session: Optional[WebSession]
) -> tuple[WebSession, str]:
    if session is None:
        session = get_web_session()
    if workspace_id is None:
        raise ValueError("Workspace selection is required.")
    return session, workspace_id


@dataclass
class ModelInfo:
    model_id: str
    name: str
    id: str = ""  # numeric internal id
    description: str = ""
    project_id: str = ""
    project_name: str = ""
    workspace_id: str = ""
    user_id: str = ""
    user_name: str = ""
    status: str = ""
    created_at: str = ""
    updated_at: str = ""
    latest_version: str = ""
    model_type: list[str] | None = None
    tags: list[str] | None = None
    model_source_path: str = ""
    model_source_type: int = 0
    raw: dict[str, Any] | None = None


def _parse_model(item: dict[str, Any]) -> ModelInfo:
    """Flatten the `/model/list` item shape (`{model: {...}, ...}`) into `ModelInfo`."""
    if not isinstance(item, dict):
        return ModelInfo(model_id="", name="")
    model_payload = item.get("model")
    inner: dict[str, Any] = model_payload if isinstance(model_payload, dict) else item
    version_value = item.get("latest_version") or item.get("next_version") or inner.get("version")
    return ModelInfo(
        model_id=str(inner.get("model_id") or inner.get("id") or ""),
        name=str(inner.get("name") or inner.get("model_name") or ""),
        id=str(inner.get("id") or ""),
        description=str(inner.get("description") or ""),
        project_id=str(inner.get("project_id") or item.get("project_id") or ""),
        project_name=str(item.get("project_name") or inner.get("project_name") or ""),
        workspace_id=str(inner.get("workspace_id") or item.get("workspace_id") or ""),
        user_id=str(inner.get("user_id") or item.get("user_id") or ""),
        user_name=str(item.get("user_name") or inner.get("user_name") or ""),
        status=str(inner.get("status") if inner.get("status") is not None else ""),
        created_at=str(inner.get("created_at") or ""),
        updated_at=str(inner.get("updated_at") or ""),
        latest_version=str(version_value or ""),
        model_type=list(inner.get("model_type") or []),
        tags=list(inner.get("tags") or []),
        model_source_path=str(inner.get("model_source_path") or ""),
        model_source_type=int(inner.get("model_source_type") or 0),
        raw=item,
    )


def _merge_filter(
    filter_by: Optional[dict[str, Any]],
    *,
    keyword: Optional[str] = None,
    user_id: Optional[str] = None,
    project_ids: Optional[Iterable[str]] = None,
    model_types: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    merged = dict(filter_by or {})
    if keyword:
        merged["keyword"] = keyword
    if user_id:
        merged["user_id"] = user_id
    if project_ids:
        values = [str(v).strip() for v in project_ids if str(v).strip()]
        if values:
            # The backend expects repeated project_id values; a bare string is
            # rejected by protobuf decoding.
            merged["project_id"] = values
    if model_types:
        values = [str(v).strip() for v in model_types if str(v).strip()]
        if values:
            merged["model_type"] = values
    return merged


def _current_user_id(session: WebSession, workspace_id: str) -> str:
    payload = _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/user?Action=GetUserDetail",
            referer=_referer(workspace_id),
            body={},
            timeout=30,
        )
    )
    user_id = str(payload.get("id") or payload.get("user_id") or "").strip()
    if not user_id:
        raise ValueError("Current user could not be resolved for model listing.")
    return user_id


def list_models(
    workspace_id: Optional[str] = None,
    *,
    page: int = 1,
    page_size: int = 100,
    filter_by: Optional[dict[str, Any]] = None,
    keyword: Optional[str] = None,
    user_id: Optional[str] = None,
    project_ids: Optional[Iterable[str]] = None,
    model_types: Optional[Iterable[str]] = None,
    session: Optional[WebSession] = None,
) -> tuple[list[ModelInfo], int]:
    """List models (`model-hub.ListModels`).

    Returns `(items, total)`. Unlike `ListImages` and `ListLogicComputeGroups`,
    this Action refuses `page_size=-1` with `InvalidParameter: page or
    page_size invalid` — page through `total` instead of asking for everything
    at once.
    """
    session, workspace_id = _resolve_workspace(workspace_id, session)
    if user_id is None:
        user_id = _current_user_id(session, workspace_id)
    body = {
        "page": page,
        "page_size": page_size,
        "filter_by": _merge_filter(
            filter_by,
            keyword=keyword,
            user_id=user_id,
            project_ids=project_ids,
            model_types=model_types,
        ),
        "workspace_id": workspace_id,
    }
    payload = _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/model-hub?Action=ListModels",
            referer=_referer(workspace_id),
            body=body,
            timeout=30,
        )
    )
    raw_items = payload.get("list") or []
    total = int(payload.get("total") or len(raw_items) or 0)
    return [_parse_model(it) for it in raw_items if isinstance(it, dict)], total


def get_model_detail(
    model_id: str,
    session: Optional[WebSession] = None,
    workspace_id: Optional[str] = None,
) -> dict[str, Any]:
    """Get model detail (`model-hub.GetModelDetail`).

    Returns the raw `data` dict — typically
    `{model: {...}, project_name, user_avatar, user_name}`.
    """
    if session is None:
        session = get_web_session()
    return _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/model-hub?Action=GetModelDetail",
            referer=_referer(workspace_id),
            body={"model_id": model_id},
            timeout=30,
        )
    )


def get_model_recommended_config(
    model_id: str,
    *,
    version: int,
    session: Optional[WebSession] = None,
    workspace_id: Optional[str] = None,
) -> dict[str, Any]:
    """Minimum viable deployment shape for a model version.

    Backs the deployment form's spec suggestion. Returns
    ``{min_node_count, min_gpu_count_per_node, min_cpu_count_per_node,
    min_memory_size_gib_per_node}`` -- a floor, not a recommendation to match
    exactly; the numbers map onto ``serving create --quota gpu,cpu,mem`` and
    ``--nodes-per-replica``.
    """
    if session is None:
        session = get_web_session()
    return _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/model-hub?Action=GetRecommendedConfig",
            referer=_referer(workspace_id),
            body={"model_id": model_id, "version": int(version)},
            timeout=30,
        )
    )


def check_model_vllm_compatible(
    model_id: str,
    *,
    version: int,
    inference_serving_type: str = "CUSTOM",
    session: Optional[WebSession] = None,
    workspace_id: Optional[str] = None,
) -> bool:
    """Whether one model version can be served by vLLM.

    :func:`get_model_vllm_compatibility` answers the same question for every
    version at once; this per-version form is the one a single deployment
    decision needs. The two agree on every version measured.
    """
    if session is None:
        session = get_web_session()
    payload = _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/model-hub?Action=CheckModelVLLMCompatible",
            referer=_referer(workspace_id),
            body={
                "model_id": model_id,
                "version": int(version),
                "inference_serving_type": inference_serving_type,
            },
            timeout=30,
        )
    )
    return payload.get("is_vllm_compatible") is True


def get_model_vllm_compatibility(
    model_id: str,
    *,
    inference_serving_type: str = "CUSTOM",
    session: Optional[WebSession] = None,
    workspace_id: Optional[str] = None,
) -> dict[int, bool]:
    """vLLM compatibility for **every** version of one model, in one request.

    ``Result.data`` is ``[{version, is_vllm_compatible}]``; this returns it
    keyed by version number.

    This -- not the record itself -- is the source for the flag. The
    ``is_vllm_compatible`` field stored on each ``ListModelVersions`` record
    reads ``false`` for every model on the platform, including the 13 of 30 that
    both this Action and ``CheckModelVLLMCompatible`` call compatible. Reading
    the stored field means answering "no" to a question nobody asked correctly.
    """
    if session is None:
        session = get_web_session()
    payload = _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/model-hub?Action=GetModelVLLMCompatibleData",
            referer=_referer(workspace_id),
            body={
                "model_id": model_id,
                "inference_serving_type": inference_serving_type,
            },
            timeout=30,
        )
    )
    rows = payload.get("data")
    compatibility: dict[int, bool] = {}
    if not isinstance(rows, list):
        return compatibility
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            version = int(str(row.get("version")))
        except (TypeError, ValueError):
            continue
        compatibility[version] = row.get("is_vllm_compatible") is True
    return compatibility


def list_model_versions(
    model_id: str,
    session: Optional[WebSession] = None,
    workspace_id: Optional[str] = None,
) -> dict[str, Any]:
    """List compact version status records via `/model/{model_id}/versions`.

    Returns the raw `data` dict (`{list: [...], total}`).
    """
    if session is None:
        session = get_web_session()
    # The compact view. `ListModelVersions` is the *richer* Action and returns
    # an extra `next_version`; that one backs `list_model_version_records`.
    return _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/model-hub?Action=ListModelVersionOptions",
            referer=_referer(workspace_id),
            body={"model_id": model_id},
            timeout=30,
        )
    )


def list_model_version_records(
    model_id: str,
    session: Optional[WebSession] = None,
    workspace_id: Optional[str] = None,
) -> dict[str, Any]:
    """List detailed version records (`model-hub.ListModelVersions`).

    This is the richer endpoint behind the model detail drawer. It includes
    model paths, source paths, sizes, publish status, and running-serving count.
    """
    if session is None:
        session = get_web_session()
    return _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/model-hub?Action=ListModelVersions",
            referer=_referer(workspace_id),
            body={"model_id": model_id},
            timeout=30,
        )
    )


def check_model_inference_serving_pending(
    *,
    model_id: str,
    version: int | str | None = None,
    session: Optional[WebSession] = None,
    workspace_id: Optional[str] = None,
) -> dict[str, Any]:
    """Whether a model has a serving queued in `PENDING`.

    Returns ``{has_pending_serving}``. The flag is true exactly when some
    serving sits in status ``PENDING``: measured against the per-version status
    distribution, ``DEPLOYING`` and ``RUNNING`` both read false. That makes it
    the complement of the ``running_infrence_serving`` count on a version
    record, which counts only ``RUNNING`` -- a queued deployment shows up in
    neither the count nor the model's own status.

    **Omitting ``version`` asks about the whole model**; the platform treats a
    missing (or ``0``) version as "any version", and a real version number
    narrows the answer to that version alone.
    """
    if session is None:
        session = get_web_session()
    body: dict[str, Any] = {"model_id": model_id}
    if version is not None:
        body["version"] = int(version)
    return _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/model-hub?Action=GetHasModelPendingServing",
            referer=_referer(workspace_id),
            body=body,
            timeout=30,
        )
    )


def list_model_inference_servings(
    *,
    model_id: str,
    version: int | str,
    page: int = 1,
    page_size: int = 10,
    session: Optional[WebSession] = None,
    workspace_id: Optional[str] = None,
) -> tuple[list[dict[str, Any]], int]:
    """List the servings that reference one model version.

    Returns ``(items, total)`` off the ``serving`` key. Each item is
    ``{name, serving_id, status, user_avatar, user_name, version}`` -- there is
    no replica count and no timestamp. Two traps:

    - ``status`` is an **int**, indexing the serving status enum
      (``PENDING``/``PRE_DEPLOYING``/``DEPLOYING``/``FAILED``/``RUNNING``/…);
      the ``inference_serving`` domain reports the same states as strings.
    - ``version`` on an item is the **serving's own** revision, not the model
      version that was asked for; the two differ in practice.

    ``version`` is required in substance: omitting it (proto default ``0``)
    returns an empty list rather than every version. ``page`` and ``page_size``
    are required outright -- omitting either is ``InvalidParameter: page or
    page_size invalid`` -- and ``page_size: -1`` is rejected the same way, so
    "fetch everything" has to be spelled as a real number.
    """
    if session is None:
        session = get_web_session()
    payload = _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/model-hub?Action=ListModelRelatedServings",
            referer=_referer(workspace_id),
            body={
                "model_id": model_id,
                "version": int(version),
                "page": page,
                "page_size": page_size,
            },
            timeout=30,
        )
    )
    items = payload.get("serving")
    if not isinstance(items, list):
        items = payload.get("inference_servings")
    if not isinstance(items, list):
        items = payload.get("list")
    if not isinstance(items, list):
        items = []
    total_raw = payload.get("total")
    try:
        total = int(str(total_raw)) if total_raw is not None else len(items)
    except ValueError:
        total = len(items)
    return [item for item in items if isinstance(item, dict)], total


def get_model_publish_prefill(
    model_id: str,
    version: int | str,
    *,
    session: Optional[WebSession] = None,
    workspace_id: Optional[str] = None,
) -> dict[str, Any]:
    """Get publish-form prefill data for one model version."""
    if session is None:
        session = get_web_session()
    data = _request_json(
        session,
        "POST",
        "/api/v2/model-hub?Action=GetModelPublishPrefill",
        referer=_referer(workspace_id),
        body={"model_id": model_id, "version": int(version)},
        timeout=30,
    )
    return _v2_result(data)


def get_model_publish_status(
    model_id: str,
    version: int | str,
    *,
    session: Optional[WebSession] = None,
    workspace_id: Optional[str] = None,
) -> dict[str, Any]:
    """Get model-plaza publish status for one model version."""
    if session is None:
        session = get_web_session()
    data = _request_json(
        session,
        "POST",
        "/api/v2/model-hub?Action=GetModelPublishStatus",
        referer=_referer(workspace_id),
        body={"model_id": model_id, "version": int(version)},
        timeout=30,
    )
    return _v2_result(data)


def list_model_users(
    project_id: str,
    *,
    session: Optional[WebSession] = None,
    workspace_id: Optional[str] = None,
) -> tuple[list[dict[str, Any]], int]:
    """List model users for a project filter (POST /model/users)."""
    if session is None:
        session = get_web_session()
    payload = _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/model-hub?Action=ListModelCreators",
            referer=_referer(workspace_id),
            body={"project_id": project_id},
            timeout=30,
        )
    )
    items = payload.get("list")
    if not isinstance(items, list):
        items = payload.get("items")
    if not isinstance(items, list):
        items = []
    total_raw = payload.get("total")
    try:
        total = int(str(total_raw)) if total_raw is not None else len(items)
    except ValueError:
        total = len(items)
    return [item for item in items if isinstance(item, dict)], total


def create_model(
    *,
    name: str,
    project_id: str,
    workspace_id: str,
    model_source_path: str,
    model_type: Optional[Iterable[str]] = None,
    tags: Optional[Iterable[str]] = None,
    description: str = "",
    model_source_type: int = 1,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Register a model in the platform model repository.

    The first version is inferred by the backend. `model_source_type=1` matches
    the UI path-registration flow for a platform-visible directory.

    Goes to `model-hub.CreateModel`, which is live but absent from discovery.
    `model_source_path` must sit under the given workspace *and* project;
    anything else — a `global_user` path included — is rejected with
    `存储路径格式不正确`.
    """
    if session is None:
        session = get_web_session()
    body = {
        "name": name,
        "project_id": project_id,
        "workspace_id": workspace_id,
        "model_source_path": model_source_path,
        "model_source_type": int(model_source_type),
        "model_type": [str(v) for v in (model_type or []) if str(v).strip()],
        "tags": [str(v) for v in (tags or []) if str(v).strip()],
        "description": description,
    }
    return _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/model-hub?Action=CreateModel",
            referer=_referer(workspace_id),
            body=body,
            timeout=60,
        )
    )


def delete_model(
    model_id: str,
    session: Optional[WebSession] = None,
    workspace_id: Optional[str] = None,
) -> dict[str, Any]:
    """Delete a model entry and every version it holds.

    Verified end to end against a self-owned model -- register, read back,
    delete, and confirm the listing no longer carries it -- rather than off the
    response envelope, which reports success either way.

    Deletion is not version-scoped: there is no per-version Action, so this
    removes the whole entry and every deployment that still points at any of
    its versions loses what it was serving. Callers are expected to ask
    :func:`list_model_inference_servings` and
    :func:`check_model_inference_serving_pending` first.

    The registered source directory on shared storage is untouched; only the
    registry entry goes away.

    There is deliberately no editing counterpart. ``model-hub.UpdateModel``
    exists but is closed to ordinary users -- on a freshly created, self-owned
    model both the ``model_id`` and the ``id`` spelling answer
    ``AccessForbidden: Access denied``, which is the permission gate refusing
    before the body is ever parsed, not a field-name problem to keep guessing at.
    """
    if session is None:
        session = get_web_session()
    return _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/model-hub?Action=DeleteModel",
            referer=_referer(workspace_id),
            body={"model_id": model_id},
            timeout=60,
        )
    )
