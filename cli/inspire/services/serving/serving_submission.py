from __future__ import annotations

import logging
import re
from typing import Any, Optional
from inspire.platform.web.session import WebSession
from inspire.config import ConfigError
from inspire.platform.web import browser_api as browser_api_module

logger = logging.getLogger(__name__)


def created_serving_id(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("inference_serving_id", "serving_id", "id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    for key in ("inference_serving", "serving", "data", "result"):
        value = created_serving_id(payload.get(key))
        if value:
            return value
    return ""


def with_tag(name: str, version: str) -> str:
    """Join an image name and its tag without doubling one that is already there.

    ``ImageInfo.name`` already carries the tag for the images the platform
    publishes -- `sandbox-base:ubuntu24.04-py3.12-1.0.0` with `version` set to
    `ubuntu24.04-py3.12-1.0.0` -- so appending it again produced
    `sandbox-base:ubuntu24.04-py3.12-1.0.0:ubuntu24.04-py3.12-1.0.0`. The
    create itself was fine (the payload carries `mirror_id`), but `--dry-run`
    and the JSON echo reported an image reference that resolves to nothing,
    which is exactly the string someone copies into a script.
    """
    if not version or name.endswith(f":{version}"):
        return name
    return f"{name}:{version}"


def resolve_image_for_create(raw: str, *, session, workspace_id: str) -> tuple[str, str]:
    """Resolve a visible image label to the `mirror_id` used by the web UI."""
    raw = (raw or "").strip()
    if not raw:
        raise ConfigError("Image is empty.")
    if raw.startswith(("image-", "mirror-")):
        raise ConfigError("--image takes a visible image name or name:tag.")
    target = raw.lower()
    for source in ("private", "public", "official"):
        try:
            images = browser_api_module.list_images_by_source(
                source=source, session=session, workspace_id=workspace_id
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("Image lookup failed for source %s: %s", source, e)
            continue
        for img in images:
            labels = {
                str(img.url or "").strip(),
                str(img.name or "").strip(),
            }
            if img.name and img.version:
                labels.add(with_tag(img.name, img.version))
            if target in {label.lower() for label in labels if label}:
                image_id = str(img.image_id or "").strip()
                if image_id:
                    display = with_tag(img.name, img.version) if img.name and img.version else raw
                    return image_id, display
                break
    raise ConfigError(f"Unknown image: {raw!r}.")


def price_value(raw_price: dict[str, Any], nested_key: str, key: str) -> Any:
    nested = raw_price.get(nested_key)
    if isinstance(nested, dict) and nested.get(key) not in (None, ""):
        return nested.get(key)
    return raw_price.get(key)


def build_resource_spec_price(resolved) -> dict[str, Any]:  # noqa: ANN001
    """Build the nested Browser API `resource_spec_price` payload."""
    raw_price = resolved.raw_price if isinstance(resolved.raw_price, dict) else {}
    payload = {
        "cpu_type": price_value(raw_price, "cpu_info", "cpu_type"),
        "cpu_count": resolved.cpu_count,
        "gpu_type": price_value(raw_price, "gpu_info", "gpu_type"),
        "gpu_count": resolved.gpu_count,
        "memory_size_gib": resolved.memory_gib,
        "logic_compute_group_id": resolved.logic_compute_group_id,
        "quota_id": resolved.quota_id,
    }
    return {key: value for key, value in payload.items() if value not in (None, "")}


def validate_custom_domain(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", text):
        raise ValueError(
            "must use lowercase letters, digits, and hyphens, and cannot start or end with a hyphen"
        )
    return text


def resolve_model_for_create(
    *,
    name: str,
    workspace_id: Optional[str],
    project_id: Optional[str],
    user_id: str,
    session,
    resolve,
) -> tuple[str, Optional[int], str]:
    # Complete the filtered catalogue before resolving: later pages can contain
    # another exact match, which must still participate in --pick ambiguity.
    items: list[browser_api_module.ModelInfo] = []
    seen: set[str] = set()
    for page in range(1, 101):
        batch, total = browser_api_module.list_models(
            workspace_id=workspace_id,
            keyword=name,
            project_ids=[project_id] if project_id else None,
            user_id=user_id,
            page=page,
            page_size=100,
            session=session,
        )
        ids = [item.model_id for item in batch]
        if any(not key or key in seen for key in ids) or len(set(ids)) != len(ids):
            raise ConfigError(
                "Could not finish model lookup: the platform returned duplicate models "
                "or missing model identities while paging. Retry serving create; if this "
                "persists, ask the platform administrator to check model catalogue pagination."
            )
        items.extend(batch)
        seen.update(ids)
        if len(items) >= total:
            break
        if not batch:
            raise ConfigError(
                "Could not finish model lookup: the platform returned an empty page "
                "before all models were read. Retry serving create; if this persists, "
                "ask the platform administrator to check model catalogue pagination."
            )
    else:
        raise ConfigError(
            "Could not finish model lookup after 100 pages (100 models requested per page). "
            "Use a more specific --model name or select a workspace with fewer matching "
            "models, then retry serving create."
        )
    candidates = [
        {
            "name": item.name,
            "id": item.model_id,
            "status": item.status,
            "created_at": item.created_at,
            "version": item.latest_version,
        }
        for item in items
    ]
    model_id = resolve(candidates)
    for item in items:
        if item.model_id == model_id:
            try:
                return (
                    model_id,
                    int(item.latest_version) if item.latest_version else None,
                    item.name,
                )
            except ValueError:
                return model_id, None, item.name
    return model_id, None, name


def create_serving(payload: dict[str, Any], *, session: WebSession | None = None) -> dict[str, Any]:
    """Unpack a planned payload for the platform create call."""
    return browser_api_module.create_serving(**payload, session=session)


def list_servings(
    *, workspace_id: str, page_num: int = 1, page_size: int = 100,
    session: WebSession | None = None,
) -> tuple[list[browser_api_module.ServingInfo], int]:
    """Translate workload paging names to the serving API contract."""
    return browser_api_module.list_servings(
        workspace_id=workspace_id, page=page_num, page_size=page_size, session=session
    )
