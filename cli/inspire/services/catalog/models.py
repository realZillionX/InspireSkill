"""Model registry, version, serving and deployment configuration views."""

from __future__ import annotations

from typing import Any, Optional

from inspire.config import ConfigError
from inspire.services.utils.raw_ids import scrub_raw_ids
from inspire.platform.web import browser_api as browser_api_module
from inspire.services.utils.text import format_epoch


def current_user_id(session) -> str:  # noqa: ANN001
    user = browser_api_module.get_current_user(session=session)
    user_id = str(user.get("id") or user.get("user_id") or "").strip()
    if not user_id:
        raise ConfigError("Cannot determine the current user from the live web session.")
    return user_id


def status_label(value: Any) -> str:
    mapping = {
        "0": "PENDING",
        "1": "CREATING",
        "2": "SUCCESS",
        "3": "FAILED",
    }
    if value is None or value == "":
        raw = ""
    else:
        raw = str(value).strip()
    return mapping.get(raw, raw or "-")


_SERVING_STATUS_LABELS = (
    "PENDING",
    "PRE_DEPLOYING",
    "DEPLOYING",
    "FAILED",
    "RUNNING",
    "SLEEPING",
    "STOPPING",
    "STOPPED",
    "QUOTA_PENDING",
)


_RELEASED_SERVING_STATUSES = frozenset({"FAILED"})


SERVING_PAGE_SIZE = 100


def _serving_status_label(value: Any) -> str:
    if isinstance(value, bool) or value is None or value == "":
        return ""
    try:
        index = int(str(value).strip())
    except (TypeError, ValueError):
        return scrub_raw_ids(value).strip()
    if 0 <= index < len(_SERVING_STATUS_LABELS):
        return _SERVING_STATUS_LABELS[index]
    return str(index)


def serving_views(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Project related servings down to what identifies and qualifies them.

    The platform hands back `serving_id` and `user_avatar` alongside the name;
    neither reaches public output. The item's own `version` is dropped too --
    it is the serving's revision, not the model version that was asked about,
    and printing it beside a model version would read as the same number.
    """
    views: list[dict[str, str]] = []
    for item in items:
        name = scrub_raw_ids(item.get("name") or "").strip()
        if not name:
            continue
        status = _serving_status_label(item.get("status"))
        if status in _RELEASED_SERVING_STATUSES:
            continue
        view = {"name": name}
        if status:
            view["status"] = status
        views.append(view)
    return views


def _format_size_gi(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number <= 0:
        return "-"
    if number >= 1024:
        return f"{number / 1024:.2f} TiB"
    return f"{number:.2f} GiB"


def version_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return f"V{text[1:]}" if text[:1].casefold() == "v" else f"V{text}"


def _string_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [scrub_raw_ids(item) for item in value if str(item or "").strip()]
    text = scrub_raw_ids(value).strip()
    return [text] if text else []


_IDENTITY_NAME_KEYS = (
    "created_by_name",
    "creator_name",
    "owner_name",
)


_IDENTITY_OBJECT_KEYS = (
    "created_by",
    "creator",
    "owner",
    "user",
)


def _explicit_identity_name(*payloads: Any) -> str:
    """Return only an explicitly projected display name from API payloads.

    The model API also exposes login-oriented scalar fields such as
    ``user_name``/``username``/``login_name``.  Those are identifiers, not
    display-name projections, so they must never be used as CLI owner text.
    Likewise, scalar ``owner``/``creator``/``created_by`` values are ignored.
    """
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in _IDENTITY_NAME_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return scrub_raw_ids(value).strip()
        for key in _IDENTITY_OBJECT_KEYS:
            identity = payload.get(key)
            if not isinstance(identity, dict):
                continue
            for name_key in ("name", "display_name"):
                value = identity.get(name_key)
                if isinstance(value, str) and value.strip():
                    return scrub_raw_ids(value).strip()
    return ""


def model_list_view(
    model: browser_api_module.ModelInfo,
    *,
    workspace: str,
) -> dict[str, str]:
    raw = model.raw if isinstance(model.raw, dict) else {}
    model_payload = raw.get("model")
    inner = model_payload if isinstance(model_payload, dict) else {}
    created_by = _explicit_identity_name(raw, inner)
    view = {
        "name": scrub_raw_ids(model.name),
        "status": scrub_raw_ids(status_label(model.status)),
        "project": scrub_raw_ids(model.project_name),
        "workspace": scrub_raw_ids(workspace),
        "version": scrub_raw_ids(version_label(model.latest_version)),
        "updated_at": scrub_raw_ids(format_epoch(model.updated_at) if model.updated_at else ""),
    }
    if created_by:
        view["created_by"] = created_by
    return {key: value for key, value in view.items() if value and value != "-"}


def version_inner(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    model_payload = item.get("model")
    return model_payload if isinstance(model_payload, dict) else item


def version_items(data: Any) -> list[dict[str, Any]]:
    items = data.get("list") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _latest_version(data: Any) -> dict[str, Any]:
    def _key(item: dict[str, Any]) -> int:
        inner = version_inner(item)
        try:
            return int(inner.get("version") or inner.get("model_version") or 0)
        except (TypeError, ValueError):
            return 0

    latest = max(version_items(data), key=_key, default={})
    return version_inner(latest)


def version_number(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if text[:1].casefold() == "v":
        text = text[1:]
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def reported_version(data: dict[str, Any], version_data: dict[str, Any]) -> Optional[int]:
    """The version number `model status` reports on, as an int."""
    model_payload = data.get("model")
    inner: dict[str, Any] = model_payload if isinstance(model_payload, dict) else data
    latest = _latest_version(version_data)
    return version_number(latest.get("version") or inner.get("version"))


def _running_serving_count(item: dict[str, Any]) -> int:
    try:
        return int(str(item.get("running_infrence_serving") or 0))
    except (TypeError, ValueError):
        return 0


def other_versions_in_use(version_data: dict[str, Any], *, reported: Optional[int]) -> list[str]:
    """Versions other than the reported one that still carry running servings.

    Free -- the count is already on each version record `model status` fetched.
    It matters because the serving list below only covers one version: deleting
    the model takes every version's deployments with it.
    """
    labels: list[str] = []
    for item in version_items(version_data):
        inner = version_inner(item)
        version = version_number(inner.get("version") or inner.get("model_version"))
        if version is None or version == reported:
            continue
        if _running_serving_count(item) <= 0:
            continue
        labels.append(version_label(version))
    return labels


def model_detail_view(
    name: str,
    data: dict[str, Any],
    version_data: dict[str, Any],
    *,
    vllm_compatibility: Optional[dict[int, bool]] = None,
) -> dict[str, Any]:
    model_payload = data.get("model")
    inner: dict[str, Any] = model_payload if isinstance(model_payload, dict) else data
    latest = _latest_version(version_data)
    version = latest.get("version") or inner.get("version")
    view: dict[str, Any] = {
        "name": scrub_raw_ids(inner.get("name") or name),
        "status": scrub_raw_ids(status_label(latest.get("status", inner.get("status")))),
        "version": version_label(version),
        "description": scrub_raw_ids(inner.get("description") or ""),
        "type": _string_values(inner.get("model_type")),
        "tags": _string_values(inner.get("tags")),
        "published": bool(inner.get("has_published")),
        "project": scrub_raw_ids(data.get("project_name") or ""),
        "owner": _explicit_identity_name(data, inner),
        "created_at": (format_epoch(inner.get("created_at")) if inner.get("created_at") else ""),
        "updated_at": (format_epoch(inner.get("updated_at")) if inner.get("updated_at") else ""),
    }
    compatibility = vllm_compatibility or {}
    number = version_number(version)
    if number is not None and number in compatibility:
        view["vllm_ready"] = compatibility[number]
    return {
        key: value
        for key, value in view.items()
        if value not in ("", None, []) or key in {"vllm_ready", "published"}
    }


def model_version_views(
    data: dict[str, Any],
    *,
    vllm_compatibility: Optional[dict[int, bool]] = None,
) -> list[dict[str, Any]]:
    compatibility = vllm_compatibility or {}
    views: list[dict[str, Any]] = []
    for item in version_items(data):
        inner = version_inner(item)
        version = inner.get("version") or inner.get("model_version")
        view: dict[str, Any] = {
            "version": version_label(version),
            "status": scrub_raw_ids(status_label(inner.get("status") or item.get("status"))),
            "size": _format_size_gi(
                inner.get("model_size_gi") or inner.get("model_size_gb") or inner.get("size")
            ),
        }
        number = version_number(version)
        if number is not None and number in compatibility:
            view["vllm_ready"] = compatibility[number]
        running = item.get("running_infrence_serving")
        if running not in (None, ""):
            view["running_servings"] = running
        views.append(
            {
                key: value
                for key, value in view.items()
                if value not in ("", None, "-") or key == "vllm_ready"
            }
        )
    return views


def model_deploy_config_view(
    name: str,
    version: int,
    recommended: dict[str, Any],
    vllm_compatible: bool,
) -> dict[str, Any]:
    view: dict[str, Any] = {
        "model": scrub_raw_ids(name),
        "version": version,
        "vllm_compatible": vllm_compatible,
    }
    for source, target in (
        ("min_node_count", "min_nodes"),
        ("min_gpu_count_per_node", "min_gpu_per_node"),
        ("min_cpu_count_per_node", "min_cpu_per_node"),
        ("min_memory_size_gib_per_node", "min_memory_gib_per_node"),
    ):
        try:
            view[target] = int(float(str(recommended.get(source))))
        except (ValueError, TypeError):
            pass
    quota_fields = ("min_gpu_per_node", "min_cpu_per_node", "min_memory_gib_per_node")
    if all(key in view for key in quota_fields):
        view["min_quota"] = ",".join(str(view[key]) for key in quota_fields)
    return view
