"""Notebook creation and observation cores shared by CLI and SDK."""

from __future__ import annotations
from typing import Any, Optional
from inspire.config import Config
from inspire.services.catalog.quotas import ResolvedQuota, build_resource_spec_price
from inspire.platform.web import browser_api as browser_api_module


def first_non_empty_str(*values: object) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def extract_notebook_id(result: object) -> str:
    if not isinstance(result, dict):
        return ""

    for key in ("notebook_id", "id", "uuid"):
        value = first_non_empty_str(result.get(key))
        if value:
            return value

    for key in ("notebook", "item", "instance"):
        nested = result.get(key)
        if isinstance(nested, dict):
            value = extract_notebook_id(nested)
            if value:
                return value
    return ""


def resolve_create_inputs(
    *,
    config: Config,
    quota: str | None,
    project: str | None,
    image: str | None,
    shm_size: int | None,
) -> tuple[str, str | None, str | None, int]:
    if not quota:
        raise ValueError("--quota is required.")
    if not project:
        raise ValueError("--project is required.")
    if not image:
        raise ValueError("--image is required.")
    if shm_size is None:
        shm_size = config.shm_size if config.shm_size is not None else 32
    if shm_size < 1:
        raise ValueError("Shared memory size must be >= 1.")
    return quota, project, image, shm_size


def split_auto_stop_after(minutes: Optional[int]) -> tuple[Optional[int], Optional[int]]:
    """Split a run duration into the hour/minute pair the platform expects.

    The console asks for 运行时长 as two numbers and refuses anything under two
    minutes; the platform only reads them while `auto_stop` is on.
    """
    if minutes is None:
        return None, None
    total = int(minutes)
    return total // 60, total % 60


def format_quota_display(quota: ResolvedQuota) -> str:
    if quota.gpu_count > 0:
        label = quota.gpu_type or "GPU"
        return f"{quota.gpu_count}x{label} + {quota.cpu_count}CPU + {quota.memory_gib}GiB"
    return f"{quota.cpu_count}CPU + {quota.memory_gib}GiB"


def find_image_match(images: list[Any], image: str) -> Any | None:
    image_lower = image.lower()
    for img in images:
        if image_lower in img.name.lower() or image_lower in img.url.lower():
            return img
    return None


def build_notebook_create_kwargs(
    *,
    name,
    project_id: str,
    project_name: str,
    image_id: str,
    image_url: str,
    quota,
    shm_size,
    auto_stop,
    workspace_id,
    task_priority=None,
    node_id=None,
    dataset_info=None,
    enable_notification=None,
    stop_hour=None,
    stop_minute=None,
    public_path_readonly=None,
    project_path_readonly=None,
) -> dict[str, Any]:
    return dict(
        name=name,
        project_id=project_id,
        project_name=project_name,
        image_id=image_id,
        image_url=image_url,
        logic_compute_group_id=quota.logic_compute_group_id,
        quota_id=quota.quota_id,
        gpu_count=quota.gpu_count,
        cpu_count=quota.cpu_count,
        memory_size=quota.memory_gib,
        shared_memory_size=shm_size,
        auto_stop=auto_stop,
        workspace_id=workspace_id,
        task_priority=task_priority,
        resource_spec_price=build_resource_spec_price(quota=quota),
        node_id=node_id,
        dataset_info=dataset_info,
        enable_notification=enable_notification,
        stop_hour=stop_hour,
        stop_minute=stop_minute,
        is_publicpath_readonly=public_path_readonly,
        is_projectuserspath_readonly=project_path_readonly,
    )


def notebook_lcg_from_detail(detail: object) -> Optional[str]:
    """Pull the compute-group handle from one notebook detail payload."""
    if not isinstance(detail, dict):
        return None
    start_cfg = detail.get("start_config")
    if isinstance(start_cfg, dict):
        lcg = start_cfg.get("logic_compute_group_id")
        if isinstance(lcg, str) and lcg.strip():
            return lcg.strip()
    grp = detail.get("logic_compute_group")
    if isinstance(grp, dict):
        for key in ("logic_compute_group_id", "compute_group_id"):
            value = grp.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def current_user_ids(session) -> list[str]:
    data = browser_api_module.get_current_user(session=session)
    value = data.get("id") or data.get("user_id")
    if not value:
        raise ValueError("Cannot list notebooks without a current-user filter.")
    return [str(value)]


def resolve_created_notebook_id(
    *, name, workspace_id, session, user_ids_loader=None, list_loader=None
) -> str:
    try:
        user_ids = user_ids_loader() if user_ids_loader else current_user_ids(session)
        if not user_ids:
            return ""
        if list_loader:
            items = list_loader(
                session, workspace_id=workspace_id, user_ids=user_ids, keyword=name, page_size=20
            )
        else:
            items = []
            for page in range(1, 101):
                rows, total = browser_api_module.list_notebooks(
                    workspace_id,
                    user_ids=user_ids,
                    keyword=name,
                    page=page,
                    page_size=20,
                    session=session,
                )
                items.extend(rows)
                if (
                    not rows
                    or (total is not None and page * 20 >= total)
                    or (total is None and len(rows) < 20)
                ):
                    break
        matches = sorted(
            (x for x in items if str(x.get("name") or "") == name),
            key=lambda x: str(x.get("created_at") or ""),
            reverse=True,
        )
        for item in matches:
            key = extract_notebook_id(item)
            if key:
                return key
    except Exception:
        pass
    return ""


def resolve_notebook_project(
    *, projects, config, project, needs_gpu_quota, workspace_id=None, session=None, api=None
):
    api = api or browser_api_module
    congested = None
    if needs_gpu_quota and workspace_id and session:
        congested = (
            api.check_scheduling_health(
                workspace_id=workspace_id,
                project_ids={p.project_id for p in projects},
                session=session,
            )
            or None
        )
    return api.select_project(
        projects,
        project,
        needs_gpu_quota=needs_gpu_quota,
        project_order=config.project_order or None,
        congested_projects=congested,
    )


def resolve_notebook_image(images, image):
    selected = find_image_match(images, image)
    if selected is None:
        raise ValueError(f"Image '{image}' not found")
    return selected


def resolve_saved_image_id(result, *, name, version, workspace_id, session, api=None) -> str:
    api = api or browser_api_module
    image_id = (result.get("image") or {}).get("image_id", "") or result.get("image_id", "")
    if image_id:
        return image_id
    try:
        matches = [
            img
            for img in api.list_images_by_source(
                source="private", session=session, workspace_id=workspace_id
            )
            if ((img.name or "").strip() == name and (img.version or "").strip() == version)
            or (img.name or "").strip() == f"{name}:{version}"
            or (img.url or "").strip().endswith(f"/{name}:{version}")
        ]
        if matches:
            return sorted(matches, key=lambda img: img.created_at or "", reverse=True)[0].image_id
    except Exception:
        pass
    return ""
