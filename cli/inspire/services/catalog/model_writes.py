from __future__ import annotations

from typing import Any, Optional
from inspire.platform.web import browser_api as browser_api_module
from inspire.services.catalog.models import (
    version_items,
    version_inner,
    version_number,
    version_label,
    serving_views,
    SERVING_PAGE_SIZE,
)
from inspire.services.utils.collections import bound_collection, DEFAULT_COLLECTION_LIMIT
from inspire.services.utils.raw_ids import scrub_raw_ids


def created_model_id(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("model_id", "id"):
        candidate = str(value.get(key) or "").strip()
        if candidate:
            return candidate
    for key in ("model", "data", "result"):
        candidate = created_model_id(value.get(key))
        if candidate:
            return candidate
    return ""


def model_references(
    model_id: str,
    version_data: dict[str, Any],
    *,
    session,  # noqa: ANN001
    workspace_id: Optional[str],
) -> list[str]:
    """Name every deployment that would break if this model went away.

    Deletion is not version-scoped, so this asks per version instead of only
    about the one `model status` reports on. The `running_infrence_serving`
    count already on each version record is not enough on its own either: it
    counts running deployments, while a stopped or sleeping serving can be
    started again and therefore still holds the version. Failed servings are
    dropped by `serving_views` -- they hold nothing.
    """
    references: list[str] = []
    for item in version_items(version_data):
        inner = version_inner(item)
        version = version_number(inner.get("version") or inner.get("model_version"))
        if version is None:
            continue
        servings, _total = browser_api_module.list_model_inference_servings(
            model_id=model_id,
            version=version,
            page=1,
            page_size=SERVING_PAGE_SIZE,
            session=session,
            workspace_id=workspace_id,
        )
        label = version_label(version)
        for serving in serving_views(servings):
            status = serving.get("status")
            suffix = f" ({status})" if status else ""
            references.append(f"{label} {serving['name']}{suffix}")
    return references


def in_use_message(name: str, references: list[str], *, pending: bool) -> str:
    """One line naming what still holds the model, within the output budget."""
    page = bound_collection(references, limit=DEFAULT_COLLECTION_LIMIT)
    parts = list(page.items)
    if page.truncated:
        parts.append(f"and {page.total - page.shown} more")
    if pending:
        parts.append("a deployment is queued on this model")
    return f"Model {scrub_raw_ids(name)} is still in use: {'; '.join(parts)}."


def model_usage(model_id, *, session, workspace_id):
    versions = browser_api_module.list_model_version_records(
        model_id=model_id, session=session, workspace_id=workspace_id
    )
    references = model_references(model_id, versions, session=session, workspace_id=workspace_id)
    pending = browser_api_module.check_model_inference_serving_pending(
        model_id=model_id, session=session, workspace_id=workspace_id
    )
    return references, pending.get("has_pending_serving") is True
