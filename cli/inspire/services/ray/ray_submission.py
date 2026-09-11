"""Ray create parsing and assembly shared by CLI and SDK."""

from __future__ import annotations
from typing import Any, Optional, cast, Callable
from inspire.config import ConfigError
import logging
from inspire.platform.web import browser_api as browser_api_module
from inspire.services.utils.raw_ids import scrub_raw_ids

logger = logging.getLogger(__name__)
IMAGE_TYPE_CHOICES = ["SOURCE_PUBLIC", "SOURCE_PRIVATE", "SOURCE_OFFICIAL"]


def parse_worker_spec(raw: str) -> dict[str, Any]:
    """Parse a ``key=value;key=value`` worker spec into a dict.

    Required keys: ``name``, ``image`` (visible image name or URL), ``group`` (compute
    group name), ``quota`` (``gpu,cpu,mem`` triple), ``min``, ``max``.
    Optional: ``image-type`` (default SOURCE_PUBLIC), ``shm-size`` (shm_gi).

    Tokens are separated by ``;`` so the ``,`` inside ``quota=4,80,800``
    doesn't collide with the outer separator.
    """
    from inspire.services.catalog.quotas import QuotaParseError, parse_quota

    out: dict[str, Any] = {}
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"worker spec token {chunk!r} has no '='; expected key=value")
        k, _, v = chunk.partition("=")
        out[k.strip()] = v.strip()

    missing = {"name", "image", "group", "quota", "min", "max"} - out.keys()
    if missing:
        raise ValueError(
            f"worker spec missing keys: {sorted(missing)}. "
            "Required: name, image, group, quota, min, max. Optional: image-type, shm-size. "
            "Format: 'name=...;image=...;group=...;quota=gpu,cpu,mem;min=N;max=N'."
        )
    try:
        out["quota_spec"] = parse_quota(out["quota"])
    except QuotaParseError as e:
        raise ValueError(f"worker quota: {e}")
    try:
        out["min"] = int(out["min"])
        out["max"] = int(out["max"])
    except ValueError as e:
        raise ValueError(f"min/max must be integers: {e}")
    if out["min"] < 1 or out["max"] < 1:
        raise ValueError("worker min and max must be >= 1.")
    if out["max"] < out["min"]:
        raise ValueError("worker max must be >= min.")
    if "image_type" in out or "shm" in out:
        raise ValueError("Use worker keys image-type and shm-size, not image_type or shm.")
    image_type = str(out.get("image-type") or "SOURCE_PUBLIC").strip()
    if image_type not in IMAGE_TYPE_CHOICES:
        raise ValueError("worker image-type must be one of " + ", ".join(IMAGE_TYPE_CHOICES) + ".")
    out["image_type"] = image_type
    out.pop("image-type", None)
    if "shm-size" in out and out["shm-size"] not in ("", None):
        try:
            out["shm_size"] = int(out["shm-size"])
        except ValueError as e:
            raise ValueError(f"shm-size must be an integer GiB value: {e}")
        if out["shm_size"] < 1:
            raise ValueError("worker shm-size must be >= 1.")
    else:
        out.pop("shm_size", None)
    out.pop("shm-size", None)
    return out


def validate_create_inputs(
    *, name: Optional[str], command: Optional[str], image: Optional[str],
    group: Optional[str], quota: Optional[str], workspace: Optional[str],
    project: Optional[str], image_type: str, workers: tuple[str, ...],
) -> None:
    if not name:
        raise ValueError("--name is required.")
    if not command:
        raise ValueError("--command is required; it is the Ray driver startup command.")
    for field_name, value in (
        ("image", image),
        ("group", group),
        ("quota", quota),
        ("workspace", workspace),
        ("project", project),
    ):
        if not value:
            raise ValueError(f"--{field_name} is required.")
    image_type_value = image_type.strip()
    if image_type_value not in IMAGE_TYPE_CHOICES:
        raise ValueError(f"--image-type must be one of: {', '.join(IMAGE_TYPE_CHOICES)}")
    if not workers:
        raise ValueError(
            "At least one --worker is required. Format: "
            "'name=<g>;image=<u>;group=<g>;quota=<gpu,cpu,mem>;min=<n>;max=<n>'"
        )


def assemble_create_body(
    *,
    workspace_id: str,
    project_id: str,
    head_quota: Any = None,
    resolve_quota: Callable[[str, str], Any],
    resolve_image: Callable[[str], str],
    resolve_priority: Callable[[Optional[int]], int],
    name: Optional[str],
    command: Optional[str],
    description: str,
    project: Optional[str],
    workspace: Optional[str],
    priority: Optional[int],
    image: Optional[str],
    image_type: str,
    group: Optional[str],
    quota: Optional[str],
    shm_size: Optional[int],
    workers: tuple[str, ...],
    public_path_readonly: Optional[bool] = None,
) -> dict[str, Any]:
    validate_create_inputs(
        name=name, command=command, image=image, group=group, quota=quota,
        workspace=workspace, project=project, image_type=image_type, workers=workers,
    )
    image_value = cast(str, image)
    image_type_value = image_type.strip()
    group_value = cast(str, group)
    quota_value = cast(str, quota)

    head_resolved = head_quota or resolve_quota(quota_value, group_value)
    head_node: dict[str, Any] = {
        "mirror_id": resolve_image(image_value),
        "image_type": image_type_value,
        "logic_compute_group_id": head_resolved.logic_compute_group_id,
        "quota_id": head_resolved.quota_id,
    }
    if shm_size is not None:
        head_node["shm_gi"] = shm_size

    worker_groups: list[dict[str, Any]] = []
    for raw in workers:
        spec = parse_worker_spec(raw)
        worker_resolved = resolve_quota(spec["quota"], spec["group"])
        group_block: dict[str, Any] = {
            "group_name": spec["name"],
            "mirror_id": resolve_image(spec["image"]),
            "image_type": spec["image_type"],
            "logic_compute_group_id": worker_resolved.logic_compute_group_id,
            "min_replicas": spec["min"],
            "max_replicas": spec["max"],
            "quota_id": worker_resolved.quota_id,
        }
        if "shm_size" in spec:
            group_block["shm_gi"] = spec["shm_size"]
        worker_groups.append(group_block)

    body: dict[str, Any] = {
        "name": name,
        "description": description,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "entrypoint": command,
        "head_node": head_node,
        "worker_groups": worker_groups,
    }
    # Only an explicit flag reaches the wire: the platform owns the default and
    # sending `false` would change every create that never asked.
    if public_path_readonly is not None:
        body["is_publicpath_readonly"] = bool(public_path_readonly)
    body["task_priority"] = resolve_priority(priority)
    return body


def resolve_image_id(
    raw: str, *, session, workspace_id: str, debug: bool = False, log: logging.Logger | None = None
) -> str:
    """Turn a visible image name or Docker image URL into the internal mirror handle.

    Ray's create body takes an internal mirror handle, not the pullable Docker
    URL. We walk public + private + official image catalogues looking for an
    exact URL/name match.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ConfigError("Image is empty.")
    target = raw.lower()
    for source in ("private", "public", "official"):
        try:
            images = browser_api_module.list_images_by_source(
                source=source, session=session, workspace_id=workspace_id
            )
        except Exception:  # noqa: BLE001
            if debug:
                (log or logger).debug("Ray image lookup via %s failed", source, exc_info=True)
            continue
        for img in images:
            labels = {
                str(img.url or "").strip(),
                str(img.name or "").strip(),
            }
            if img.name and img.version:
                labels.add(f"{img.name}:{img.version}")
            if target in {label.lower() for label in labels if label}:
                return img.image_id
    display = scrub_raw_ids(raw)
    raise ConfigError(
        f"Image {display!r} not found in public/private/official catalogues. "
        "Pass a visible image name or Docker URL from `inspire image list`."
    )


def ray_plan_payload(
    *,
    body: dict[str, Any],
    workspace: str,
    project: str,
    group: str,
    quota: str,
    image: str,
    workers: tuple[str, ...],
    description: str,
    shm_size: int | None,
    public_path_readonly: bool | None,
) -> dict[str, Any]:
    from inspire.services.catalog.quotas import parse_quota

    head_spec = parse_quota(cast(str, quota))
    worker_plans: list[dict[str, Any]] = []
    for raw_worker in workers:
        worker = parse_worker_spec(raw_worker)
        worker_spec = worker["quota_spec"]
        worker_plan: dict[str, Any] = {
            "name": worker["name"],
            "compute_group": worker["group"],
            "resource": {
                "gpu": worker_spec.gpu_count,
                "cpu": worker_spec.cpu_count,
                "memory_gib": worker_spec.memory_gib,
            },
            "image": worker["image"],
            "min_replicas": worker["min"],
            "max_replicas": worker["max"],
        }
        if worker.get("shm_size") is not None:
            worker_plan["shared_memory_gib"] = worker["shm_size"]
        worker_plans.append(worker_plan)
    plan: dict[str, Any] = {
        "dry_run": True,
        "name": body.get("name"),
        "workspace": workspace,
        "project": project,
        "compute_group": group,
        "resource": {
            "gpu": head_spec.gpu_count,
            "cpu": head_spec.cpu_count,
            "memory_gib": head_spec.memory_gib,
        },
        "image": image,
        "command": body.get("entrypoint"),
        "priority": body.get("task_priority"),
        "workers": worker_plans,
    }
    if description:
        plan["description"] = description
    if shm_size is not None:
        plan["shared_memory_gib"] = shm_size
    if public_path_readonly is not None:
        plan["public_path_readonly"] = bool(public_path_readonly)
    return plan


def created_ray_job_id(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("ray_job_id", "job_id", "id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    for key in ("ray_job", "job", "data", "result"):
        value = created_ray_job_id(payload.get(key))
        if value:
            return value
    return ""
