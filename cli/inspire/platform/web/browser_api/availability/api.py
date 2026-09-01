"""Browser (web-session) APIs for compute group availability (HTTP endpoints)."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .models import (
    FullFreeNodeCount,
    GPUAvailability,
    MemberUsage,
    NodeSpec,
    TaskUsage,
)
from inspire.platform.web.browser_api.core import (
    _coerce_total,
    _get_base_url,
    _request_json,
    _v2_result,
)
from inspire.platform.web.session import (
    SessionExpiredError,
    WebSession,
    get_web_session,
)


def list_compute_groups(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List compute groups using the browser API."""
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        raise ValueError("Workspace selection is required.")

    # `page_size: -1` means "all" and v2 honours it. Keep it: omitting
    # `page_size` entirely makes v2 return an empty list with a non-zero
    # `total`, which would silently look like a workspace with no groups.
    body = {
        "page_size": -1,
        "page_num": 1,
        "filter": {"workspace_id": workspace_id},
    }

    # workspace.*, never cluster.* — the cluster twin of this Action answers
    # AccessForbidden to anyone who is not a cluster admin.
    payload = _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/workspace?Action=ListLogicComputeGroups",
            referer=f"{_get_base_url()}/jobs/distributedTraining",
            body=body,
            timeout=30,
        )
    )
    groups = payload.get("logic_compute_groups")
    if not isinstance(groups, list):
        raise ValueError("ListLogicComputeGroups response omitted logic_compute_groups.")
    return groups


# Workload -> the `notebook.GetScheduleConfig` key carrying its spec menu.
# HPC is deliberately absent: the shared record has no HPC menu at all (its
# `predef_node_spec` lives in `hpc.GetHpcScheduleConfig`), so HPC quotas have
# no published priority restriction to join against.
QUOTA_PRIORITY_SPEC_FIELDS: dict[str, str] = {
    "notebook": "quota",
    "job": "predef_train_spec",
    "ray": "rayjob_quota",
    "serving": "serving_quota",
}


def get_quota_priority_levels(
    workspace_id: str,
    *,
    spec_field: str,
    session: Optional[WebSession] = None,
) -> dict[str, tuple[str, ...]]:
    """Read which task priorities a workspace lets each quota spec use.

    Action: ``notebook.GetScheduleConfig``, body ``{"WorkspaceId": ...}`` in
    PascalCase — the sibling Actions of that family take snake_case and reject
    this spelling. One request answers the whole workspace, because the shared
    record carries a separate spec menu per workload; there is no per-compute-
    group fan-out here and adding one would only multiply the rate-limit risk.

    ``workspace.GetScheduleConfig`` looks like the Action for this and is not:
    it answers ``AccessForbidden: You are not the admin of the <workspace_id>
    workspace`` to an ordinary member. The notebook Action is member-readable.

    Each menu arrives **JSON-encoded as a string**, not as an array, and each
    element's ``id`` is the handle the price rows spell ``quota_id`` — that
    is the join, and it is exact: measured across every visible workspace and
    workload, all 96+ live price rows found their spec. The payload is
    ``allowed_priority_levels``: ``null`` or ``[]`` for a spec the workspace
    schedules at any priority, ``["low"]`` for one it will only schedule at low
    priority.

    Returns ``{quota_id: levels}``, with an empty tuple meaning "no
    restriction". A spec id absent from the result is one this workspace said
    nothing about, and callers must not read silence as permission.
    """
    if session is None:
        session = get_web_session()
    if not workspace_id:
        raise ValueError("Workspace selection is required.")
    field = str(spec_field or "").strip()
    if not field:
        raise ValueError("A schedule-config spec field is required.")

    config = _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/notebook?Action=GetScheduleConfig",
            referer=f"{_get_base_url()}/jobs/interactiveModeling",
            body={"WorkspaceId": workspace_id},
            timeout=20,
        )
    )

    menu = config.get(field)
    if isinstance(menu, str):
        text = menu.strip()
        if not text:
            return {}
        try:
            menu = json.loads(text)
        except ValueError:
            return {}
    if not isinstance(menu, list):
        return {}

    levels_by_quota_id: dict[str, tuple[str, ...]] = {}
    for spec in menu:
        if not isinstance(spec, dict):
            continue
        quota_id = str(spec.get("id") or "").strip()
        if not quota_id:
            continue
        allowed = spec.get("allowed_priority_levels")
        if not isinstance(allowed, list):
            # `null` and a missing key both mean "no restriction declared",
            # which the platform also spells as an empty list.
            allowed = []
        levels_by_quota_id[quota_id] = tuple(
            sorted({str(level).strip().lower() for level in allowed if str(level).strip()})
        )
    return levels_by_quota_id


def _group_id(group: dict) -> str:
    return str(group.get("logic_compute_group_id") or group.get("id") or "").strip()


def _group_name(group: dict) -> str:
    return str(
        group.get("name")
        or group.get("logic_compute_group_name")
        or group.get("compute_group_name")
        or ""
    ).strip()


def list_node_events(
    node_names: list[str],
    *,
    page_size: int = 200,
    max_pages: int | None = None,
    sort_ascending: bool = True,
    from_component: str | None = None,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List Kubernetes events belonging to nodes themselves.

    Action: ``cluster.ListNodeEvents``. This is the only event source on the
    platform keyed by node rather than by workload — kernel OOM kills, cordons,
    ``NodeNotSchedulable``, controller removals — and it answers for ordinary
    workspace members, unlike the rest of the ``cluster.*`` surface.

    ``filter.node_names`` is required in practice: an empty filter answers
    ``{"events": [], "total": 0}``, which reads as "this cluster is quiet"
    rather than "you asked nothing". Several node names in one call are fine,
    and every row carries its own ``node_name``. A node name the cluster does
    not know is likewise an empty list, not an error.

    Rows are Kubernetes-shaped but spelled differently from every other event
    Action here: the type field is ``event_type`` (not ``type``) and there is
    no ``count``. ``filter.from`` narrows by reporting component; ``event_type``
    / ``type`` / ``keyword`` are all ``unknown field``, and the declared
    ``start_last_timestamp`` / ``end_last_timestamp`` pair answers
    ``InternalError``, so time windowing stays on this side.
    """
    clean_names = list(
        dict.fromkeys(
            str(name or "").strip() for name in node_names if str(name or "").strip()
        )
    )
    if not clean_names:
        raise ValueError("Node selection is required.")
    if page_size < 1:
        raise ValueError("page_size must be positive")
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be positive")
    if session is None:
        session = get_web_session()

    sort = "ascend" if sort_ascending else "descend"
    filters: dict[str, object] = {"node_names": clean_names}
    component = str(from_component or "").strip()
    if component:
        filters["from"] = component
    events: list[dict] = []
    page = 1
    while max_pages is None or page <= max_pages:
        payload = _v2_result(
            _request_json(
                session,
                "POST",
                "/api/v2/cluster?Action=ListNodeEvents",
                referer=f"{_get_base_url()}/cluster/nodeList",
                body={
                    "PageNumber": page,
                    "page_size": page_size,
                    "filter": dict(filters),
                    "sorter": [{"field": "last_timestamp", "sort": sort}],
                },
                timeout=30,
            )
        )
        page_events = payload.get("events")
        if not isinstance(page_events, list):
            raise ValueError("ListNodeEvents response omitted events.")
        events.extend(item for item in page_events if isinstance(item, dict))

        total = _coerce_total(payload.get("total"), -1)
        if not page_events or (total >= 0 and len(events) >= total):
            break
        if total < 0 and len(page_events) < page_size:
            break
        page += 1
    return events


def list_node_dimension(
    logic_compute_group_id: str,
    *,
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
    page_size: int = 500,
) -> list[dict]:
    """List live node dimensions for one compute group.

    Action: ``workspace.ListNodeDimension``. Both the workspace and the group
    must be inside ``filter``; passing the group alone answers
    ``AccessForbidden``, which reads as a permission problem but is really the
    scoping trap.

    Unlike ``ListLogicComputeGroups``, ``page_size: -1`` here does **not** mean
    "all" — it returns 10 rows — so this pages explicitly against ``total``.

    Each row carries live state (``status``, ``tasks_associated``,
    ``cordon_type``, ``resource_pool``) with GPU counts nested under ``gpu``.
    """
    if session is None:
        session = get_web_session()
    if workspace_id is None:
        raise ValueError("Workspace selection is required.")

    nodes: list[dict] = []
    seen: set[str] = set()
    page = 1
    while True:
        payload = _v2_result(
            _request_json(
                session,
                "POST",
                "/api/v2/workspace?Action=ListNodeDimension",
                referer=f"{_get_base_url()}/jobs/distributedTraining",
                body={
                    "filter": {
                        "workspace_id": workspace_id,
                        "logic_compute_group_id": logic_compute_group_id,
                    },
                    "PageNumber": page,
                    "page_size": max(1, int(page_size)),
                },
                timeout=30,
            )
        )
        rows = payload.get("node_dimensions")
        if not isinstance(rows, list):
            raise ValueError("ListNodeDimension response omitted node_dimensions.")
        if not rows:
            break
        before_count = len(nodes)
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("id") or row.get("name") or row.get("node_name") or "")
            if key:
                if key in seen:
                    continue
                seen.add(key)
            nodes.append(row)

        try:
            total = int(str(payload.get("total")))
        except (TypeError, ValueError):
            break
        if len(nodes) >= total:
            break
        if len(nodes) == before_count:
            break
        page += 1
        if page > _NODE_DIMENSION_PAGE_CAP:
            raise ValueError(
                "Node dimension exceeded the safe pagination limit before "
                "the platform-reported total was read."
            )

    return nodes


_DIMENSION_PAGE_SIZE = 5000
_DIMENSION_PAGE_CAP = 40
_NODE_DIMENSION_PAGE_CAP = 100


def _list_dimension_rows(
    action: str,
    list_key: str,
    *,
    workspace_id: str,
    session: WebSession,
    logic_compute_group_id: Optional[str] = None,
    user_id: Optional[str] = None,
    page_size: int = _DIMENSION_PAGE_SIZE,
) -> list[dict]:
    """Page one ``workspace.List*Dimension`` Action to completion.

    The whole family scopes through nested ``filter.workspace_id``; a top-level
    ``workspace_id`` is rejected outright as an unknown field, and dropping the
    workspace answers ``AccessForbidden`` rather than an empty list.

    They also share ``ListNodeDimension``'s paging trap: ``page_size: -1`` and
    an omitted ``page_size`` both answer exactly 10 rows instead of everything,
    so this pages explicitly against ``total``. Server-side ``order_by`` is not
    a way out — only ``created_at`` is honoured, its ``sort`` direction is
    ignored, and ordering by a resource field answers ``InternalError``.
    """
    filters: dict[str, object] = {"workspace_id": workspace_id}
    if logic_compute_group_id:
        filters["logic_compute_group_id"] = logic_compute_group_id
    if user_id:
        filters["user_id"] = user_id

    rows: list[dict] = []
    seen: set[str] = set()
    page = 1
    while True:
        payload = _v2_result(
            _request_json(
                session,
                "POST",
                f"/api/v2/workspace?Action={action}",
                referer=f"{_get_base_url()}/jobs/distributedTraining",
                body={
                    "filter": dict(filters),
                    "PageNumber": page,
                    "page_size": max(1, int(page_size)),
                },
                timeout=60,
            )
        )
        batch = payload.get(list_key)
        if not isinstance(batch, list):
            raise ValueError(f"{action} response omitted {list_key}.")
        if not batch:
            break
        before_count = len(rows)
        for row in batch:
            if not isinstance(row, dict):
                continue
            # The list churns while it is being paged, so a row can repeat
            # across page boundaries and inflate every aggregate built on it.
            key = str(row.get("id") or "")
            if key:
                if key in seen:
                    continue
                seen.add(key)
            rows.append(row)

        total = _coerce_total(payload.get("total"), len(rows))
        if len(rows) >= total:
            break
        if len(rows) == before_count:
            break
        page += 1
        if page > _DIMENSION_PAGE_CAP:
            raise ValueError(
                f"{action} exceeded the safe pagination limit before the "
                "platform-reported total was read."
            )

    return rows


def _block_amount(row: dict, block: str, key: str = "total") -> float:
    values = row.get(block)
    if not isinstance(values, dict):
        return 0.0
    try:
        return float(values.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _occupied_nodes(row: dict, key: str) -> tuple[str, ...]:
    occupied = row.get(key)
    if not isinstance(occupied, dict):
        return ()
    names = occupied.get("nodes")
    if not isinstance(names, list):
        return ()
    return tuple(str(name) for name in names if name)


def _occupied_node_count(row: dict, key: str) -> int:
    occupied = row.get(key)
    if not isinstance(occupied, dict):
        return 0
    try:
        return int(occupied.get("count") or 0)
    except (TypeError, ValueError):
        return 0


def _named(row: dict, key: str, *name_keys: str) -> str:
    nested = row.get(key)
    if not isinstance(nested, dict):
        return ""
    for name_key in name_keys:
        value = nested.get(name_key)
        if value:
            return str(value)
    return ""


def list_task_usage(
    workspace_id: str,
    *,
    logic_compute_group_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[TaskUsage]:
    """List every live workload in a workspace with the capacity it holds.

    Action: ``workspace.ListTaskDimension``. This is the workspace-wide view —
    it carries every member's tasks, not just the caller's — which makes it the
    only read that answers who took the capacity that `resources availability`
    reports as gone.

    Rows are live workloads only (``RUNNING`` plus short-lived transitions such
    as ``COMMITTING``); finished tasks never appear.
    """
    if session is None:
        session = get_web_session()
    if not workspace_id:
        raise ValueError("Workspace selection is required.")

    rows = _list_dimension_rows(
        "ListTaskDimension",
        "task_dimensions",
        workspace_id=workspace_id,
        session=session,
        logic_compute_group_id=logic_compute_group_id,
    )

    usages: list[TaskUsage] = []
    for row in rows:
        usages.append(
            TaskUsage(
                task_id=str(row.get("id") or ""),
                name=str(row.get("name") or ""),
                task_type=str(row.get("type") or ""),
                status=str(row.get("status") or ""),
                user_name=_named(row, "user", "name", "name_en"),
                project_name=_named(row, "project", "name", "project_name"),
                gpus=int(_block_amount(row, "gpu")),
                cpus=_block_amount(row, "cpu"),
                memory_gib=_block_amount(row, "memory"),
                gpu_usage_rate=_block_amount(row, "gpu", "usage_rate"),
                cpu_usage_rate=_block_amount(row, "cpu", "usage_rate"),
                node_names=_occupied_nodes(row, "nodes_occupied"),
                created_at=str(row.get("created_at") or ""),
                running_time_ms=int(_coerce_total(row.get("running_time_ms"), 0)),
                priority=int(_coerce_total(row.get("priority"), 0)),
            )
        )
    return usages


def list_member_usage(
    workspace_id: str,
    *,
    session: Optional[WebSession] = None,
) -> list[MemberUsage]:
    """List the caller's own footprint in a workspace, split by project.

    Action: ``workspace.ListUserDimension``. An unfiltered request is a
    workspace-wide per-user view, so this wrapper must pass the authenticated
    user's id inside ``filter``. Without it, `resources usage --mine` labels
    every member's rows as the caller's own footprint.

    The filtered result remains useful because it is one pre-aggregated
    request, where the same answer from the task dimension costs a full paged
    sweep of every workload in the workspace.
    """
    if session is None:
        session = get_web_session()
    if not workspace_id:
        raise ValueError("Workspace selection is required.")

    # Local import avoids a browser_api package initialization cycle.
    from inspire.platform.web.browser_api.jobs import get_current_user

    current_user = get_current_user(session)
    user_id = str(current_user.get("id") or current_user.get("user_id") or "").strip()
    if not user_id:
        raise ValueError("Could not resolve the current user for resource usage.")

    rows = _list_dimension_rows(
        "ListUserDimension",
        "user_dimensions",
        workspace_id=workspace_id,
        session=session,
        user_id=user_id,
    )

    return [
        MemberUsage(
            user_name=_named(row, "user", "name", "name_en"),
            project_name=_named(row, "project", "project_name", "name"),
            gpus=int(_block_amount(row, "gpu")),
            cpus=_block_amount(row, "cpu"),
            memory_gib=_block_amount(row, "memory"),
            gpu_nodes=_occupied_node_count(row, "gpu_nodes_occupied"),
            cpu_nodes=_occupied_node_count(row, "cpu_nodes_occupied"),
            hpc_nodes=_occupied_node_count(row, "hpc_nodes_occupied"),
        )
        for row in rows
    ]


def list_node_specs(
    workspace_id: str,
    *,
    logic_compute_group_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[NodeSpec]:
    """List the distinct per-node hardware shapes reachable in a workspace.

    Actions: ``workspace.GetLogicComputeGroupNodeSpecs`` when a group is given,
    ``workspace.GetWorkspaceNodeSpecs`` otherwise. Both take scoping at the
    **top level** — nesting it under ``filter`` is rejected as an unknown field,
    and sending the group without the workspace answers ``AccessForbidden``.

    The platform returns one row per (shape x supported job type) and repeats
    shapes that differ only in fractions of a GiB, so 68 raw rows collapse to 6
    real shapes. Rows are folded here, and the fold is by shape, never by node:
    a 292-node group publishes 17 shapes, so a count of these rows would be a
    fabricated node count. Largest shape first, because the question this
    answers is whether a requested ``gpu,cpu,mem`` triple fits on any one node.
    """
    if session is None:
        session = get_web_session()
    if not workspace_id:
        raise ValueError("Workspace selection is required.")

    if logic_compute_group_id:
        action = "GetLogicComputeGroupNodeSpecs"
        body: dict[str, object] = {
            "workspace_id": workspace_id,
            "logic_compute_group_id": logic_compute_group_id,
        }
    else:
        action = "GetWorkspaceNodeSpecs"
        body = {"workspace_id": workspace_id}

    payload = _v2_result(
        _request_json(
            session,
            "POST",
            f"/api/v2/workspace?Action={action}",
            referer=f"{_get_base_url()}/jobs/distributedTraining",
            body=body,
            timeout=30,
        )
    )
    rows = payload.get("node_specs")
    if not isinstance(rows, list):
        raise ValueError(f"{action} response omitted node_specs.")

    folded: dict[tuple, set[str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        gpu_info = row.get("gpu_info")
        # The flat `gpu_type` is empty and `gpu_memory_size` is 0 on every live
        # row; the readable model name only exists inside `gpu_info`.
        gpu_type = ""
        if isinstance(gpu_info, dict):
            gpu_type = str(
                gpu_info.get("gpu_product_simple")
                or gpu_info.get("gpu_type_display")
                or gpu_info.get("gpu_type")
                or ""
            )
        gpu_type = gpu_type or str(row.get("gpu_type") or "")

        try:
            gpu_count = int(float(row.get("gpu_count") or 0))
            cpu_count = float(row.get("cpu_count") or 0)
            memory_gib = float(row.get("memory_size") or 0)
        except (TypeError, ValueError):
            continue

        shape = (
            str(row.get("node_type") or ""),
            gpu_type,
            gpu_count,
            float(int(cpu_count)),
            float(int(memory_gib)),
        )
        job_type = str(row.get("support_job_type") or "").strip()
        bucket = folded.setdefault(shape, set())
        if job_type:
            bucket.add(job_type)

    specs = [
        NodeSpec(
            node_type=shape[0],
            gpu_type=shape[1],
            gpu_count=shape[2],
            cpu_count=shape[3],
            memory_gib=shape[4],
            job_types=tuple(sorted(job_types)),
        )
        for shape, job_types in folded.items()
    ]
    specs.sort(
        key=lambda spec: (spec.gpu_count, spec.cpu_count, spec.memory_gib),
        reverse=True,
    )
    return specs


def _list_live_compute_groups(
    *,
    workspace_id: str,
    session: WebSession,
) -> list[dict]:
    return list_compute_groups(workspace_id=workspace_id, session=session)


def _node_gpu_total(node: dict) -> int:
    """GPU count for one node row.

    `ListNodeDimension` nests it under ``gpu.total``; the older node-spec rows
    carried a flat ``gpu_count`` / ``gpu_total``. Reading only the flat keys
    made every dimension row look like a zero-GPU node, which silently zeroed
    the free-node counts.
    """
    gpu = node.get("gpu")
    if isinstance(gpu, dict):
        try:
            return int(gpu.get("total") or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(node.get("gpu_count") or node.get("gpu_total") or 0)
    except (TypeError, ValueError):
        return 0


def _node_gpu_used(node: dict) -> int:
    gpu = node.get("gpu")
    if not isinstance(gpu, dict):
        return 0
    try:
        return int(gpu.get("used") or 0)
    except (TypeError, ValueError):
        return 0


def _node_task_associations(node: dict) -> tuple[tuple[dict, ...], int]:
    """Return visible task rows and the platform-declared association count.

    Live ``tasks_associated`` is a container shaped like
    ``{"count": N, "tasks": [...]}``, not the task list itself.  Treating the
    container's keys as occupants makes even ``count=0`` truthy and therefore
    hides every genuinely idle node.  Older fixtures and responses can still
    carry a bare list under ``task_list``, so both shapes remain readable.

    The declared count is kept separate from the visible rows: if the platform
    says tasks exist but omits their identities, callers must classify the
    node as busy with unknown priority rather than free or reclaimable.
    """
    entries: list[dict] = []
    declared_count = 0
    for key in ("task_list", "tasks_associated"):
        raw = node.get(key)
        if isinstance(raw, list):
            visible = [item for item in raw if isinstance(item, dict)]
            entries.extend(visible)
            declared_count = max(declared_count, len(raw))
            continue
        if not isinstance(raw, dict):
            continue
        tasks = raw.get("tasks")
        visible = (
            [item for item in tasks if isinstance(item, dict)]
            if isinstance(tasks, list)
            else []
        )
        entries.extend(visible)
        try:
            count = int(raw.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        declared_count = max(declared_count, count, len(visible))
    return tuple(entries), declared_count


def _node_is_schedulable(node: dict) -> bool:
    """Whether scheduler state permits placing a workload on this node.

    ``READY`` alone is not enough: a cordoned, under-maintenance, or faulted
    node keeps reporting idle cards it will never schedule onto.
    """
    if str(node.get("status") or "").upper() != "READY":
        return False
    if str(node.get("cordon_type") or "").strip():
        return False
    if bool(node.get("is_maint", False)):
        return False
    return str(node.get("resource_pool") or "").lower() != "fault"


def _node_is_schedulable_and_idle(node: dict) -> bool:
    """Whether a node can take work now and holds no allocation."""
    _tasks, declared_count = _node_task_associations(node)
    return (
        _node_is_schedulable(node)
        and declared_count == 0
        and _node_gpu_used(node) == 0
    )


def _compute_node_summary(nodes: list[dict]) -> dict[str, int]:
    total_nodes = 0
    ready_nodes = 0
    free_nodes = 0
    gpu_per_node = 0

    for node in nodes:
        # A fair-scheduling node can report a zero guarantee (`total=0`) while
        # a low-priority task is using all eight physical GPUs.  `used` is then
        # the only positive GPU signal, so taking only `total` drops a real node
        # from Total/Ready and makes availability disagree with `resources nodes`.
        gpu_total = _node_gpu_total(node)
        gpu_used = _node_gpu_used(node)
        if max(gpu_total, gpu_used) <= 0:
            continue
        total_nodes += 1
        # `used` may itself exceed physical capacity under logical overcommit
        # (16 has been observed on an 8-GPU H100 node), so it is only a node-
        # presence signal and must never define the per-node hardware shape.
        gpu_per_node = max(gpu_per_node, gpu_total)

        if str(node.get("status") or "").upper() == "READY":
            ready_nodes += 1

        if _node_is_schedulable_and_idle(node):
            free_nodes += 1

    return {
        "total_nodes": total_nodes,
        "ready_nodes": ready_nodes,
        "free_nodes": free_nodes,
        "gpu_per_node": gpu_per_node,
    }


def get_accurate_resource_availability(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
    *,
    include_cpu: bool = False,
) -> list[GPUAvailability]:
    """Get accurate compute-group availability, optionally including CPU-only groups."""
    if session is None:
        session = get_web_session()

    if not workspace_id:
        raise ValueError("Workspace selection is required.")
    workspace_ids = [workspace_id]
    workspace_names = session.all_workspace_names or {}

    try:
        results: list[GPUAvailability] = []
        for wid in workspace_ids:
            groups = _list_live_compute_groups(workspace_id=wid, session=session)
            workspace_name = workspace_names.get(wid, "")

            def _load_group(group: dict) -> tuple[dict, dict, dict[str, int], list[dict]]:
                group_id = _group_id(group)
                if not group_id:
                    raise ValueError("Compute-group response omitted logic_compute_group_id.")
                if not _group_name(group):
                    raise ValueError("Compute-group response omitted its visible name.")

                group_resource = _v2_result(
                    _request_json(
                        session,
                        "POST",
                        "/api/v2/workspace?Action=GetLogicComputeGroupResource",
                        referer=f"{_get_base_url()}/jobs/distributedTraining",
                        body={
                            "workspace_id": wid,
                            "logic_compute_group_id": group_id,
                        },
                        timeout=30,
                    )
                )
                node_dimensions = list_node_dimension(
                    group_id,
                    workspace_id=wid,
                    session=session,
                )
                node_summary = _compute_node_summary(node_dimensions)
                return group, group_resource, node_summary, node_dimensions

            max_workers = min(4, len(groups)) or 1
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                loaded_groups = list(executor.map(_load_group, groups))

            for loaded in loaded_groups:
                group, group_resource, node_summary, node_dimensions = loaded
                group_id = _group_id(group)
                group_name = _group_name(group)

                # Platform spelling: `logic_resouces`, not `logic_resources`.
                resources = group_resource.get("logic_resouces", {})
                gpu_stats = group_resource.get("gpu_type_stats", [{}])

                gpu_info: dict = {}
                if isinstance(gpu_stats, list) and gpu_stats:
                    first_gpu_stat = gpu_stats[0]
                    if isinstance(first_gpu_stat, dict):
                        raw_gpu_info = first_gpu_stat.get("gpu_info")
                        if isinstance(raw_gpu_info, dict):
                            gpu_info = raw_gpu_info
                gpu_type = str(gpu_info.get("gpu_type_display") or "").strip()
                gpu_type_code = str(gpu_info.get("gpu_type") or "").strip().upper()

                gpu_total = int(resources.get("gpu_total", 0) or 0)
                gpu_used = int(resources.get("gpu_used", 0) or 0)
                gpu_low_priority = int(resources.get("gpu_low_priority_used", 0) or 0)
                gpu_available = gpu_total - gpu_used

                cpu_total = float(resources.get("cpu_total", 0) or 0)
                cpu_used = float(resources.get("cpu_used", 0) or 0)
                cpu_available = cpu_total - cpu_used

                memory_total_gib = float(resources.get("memory_gi_total", 0) or 0)
                memory_used_gib = float(resources.get("memory_gi_used", 0) or 0)
                memory_available_gib = memory_total_gib - memory_used_gib

                # `gpu_total` is the workspace's guaranteed amount, not a
                # hardware classifier. Fair-scheduling groups can publish a
                # zero guarantee while live node dimensions and usage show
                # real GPUs; classifying on total alone hid those groups from
                # the default GPU view. A negative available value is still a
                # useful fact here: it says usage is beyond the guarantee.
                has_gpu_type = bool(
                    gpu_type_code
                    and gpu_type_code not in {"CPU", "GPU_TYPE_UNSPECIFIED", "UNSPECIFIED"}
                )
                has_gpu_signal = (
                    gpu_total > 0
                    or gpu_used > 0
                    or gpu_low_priority > 0
                    or node_summary["gpu_per_node"] > 0
                    or has_gpu_type
                )
                resource_kind = "gpu" if has_gpu_signal else "cpu"
                if resource_kind == "cpu" and not include_cpu:
                    continue
                if resource_kind == "cpu":
                    has_any_cpu_signal = any(
                        value > 0
                        for value in (cpu_total, cpu_used, memory_total_gib, memory_used_gib)
                    )
                    if not has_any_cpu_signal:
                        continue

                results.append(
                    GPUAvailability(
                        group_id=group_id,
                        group_name=group_name,
                        gpu_type=gpu_type,
                        total_gpus=gpu_total,
                        used_gpus=gpu_used,
                        available_gpus=gpu_available,
                        low_priority_gpus=gpu_low_priority,
                        total_nodes=node_summary["total_nodes"],
                        ready_nodes=node_summary["ready_nodes"],
                        free_nodes=node_summary["free_nodes"],
                        gpu_per_node=node_summary["gpu_per_node"],
                        workspace_id=wid,
                        workspace_name=workspace_name,
                        cpu_total=cpu_total,
                        cpu_used=cpu_used,
                        cpu_available=cpu_available,
                        memory_total_gib=memory_total_gib,
                        memory_used_gib=memory_used_gib,
                        memory_available_gib=memory_available_gib,
                        resource_kind=resource_kind,
                        node_dimensions=tuple(node_dimensions),
                    )
                )

        return results

    except SessionExpiredError:
        # request_json() owns the one authentication boundary and has already
        # spent it. Restarting the whole fan-out here only adds a second login
        # to a session the platform has now refused twice.
        raise


def get_accurate_gpu_availability(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[GPUAvailability]:
    """Get accurate GPU availability for all compute groups."""
    results = get_accurate_resource_availability(
        workspace_id=workspace_id,
        session=session,
        include_cpu=False,
    )
    return [row for row in results if row.resource_kind == "gpu"]


def get_full_free_node_counts(
    group_ids: list[str],
    *,
    gpu_per_node: int = 8,
    workspace_id_by_group: Optional[dict[str, str]] = None,
    node_dimensions_by_group: Optional[dict[str, list[dict]]] = None,
    low_priority_task_ids: Optional[set[str]] = None,
    session: Optional[WebSession] = None,
) -> list[FullFreeNodeCount]:
    """Get per-group whole-node capacity now and after low-priority preemption.

    Backed by ``workspace.ListNodeDimension``, which ordinary members can read
    — the admin-only node listings cannot, and reading one of those is how the
    free-node column silently read zero for them.

    Node rows do not carry priority.  The caller may therefore pass task ids
    classified from the same workspace's live ``ListTaskDimension`` response.
    A busy node is reclaimable only when every declared occupant is visible,
    has a stable task id, and belongs to that set.  Churn or missing priority is
    deliberately conservative.
    """
    if session is None:
        session = get_web_session()

    by_group = dict(workspace_id_by_group or {})
    prefetched_nodes = dict(node_dimensions_by_group or {})
    low_task_ids = set(low_priority_task_ids or set())
    fallback_workspace = str(getattr(session, "workspace_id", "") or "").strip()
    results: list[FullFreeNodeCount] = []

    try:
        for gid in group_ids:
            workspace_id = by_group.get(gid) or fallback_workspace
            if not workspace_id:
                continue

            nodes = prefetched_nodes.get(gid)
            if nodes is None:
                nodes = list_node_dimension(
                    gid,
                    workspace_id=workspace_id,
                    session=session,
                )

            total_nodes = len(nodes)
            ready_nodes = 0
            full_free_nodes = 0
            reclaimable_nodes = 0
            group_name = ""

            for node in nodes:
                if not group_name:
                    group_name = str(node.get("logic_compute_group_name") or "")

                if str(node.get("status") or "").upper() != "READY":
                    continue
                ready_nodes += 1

                if _node_gpu_total(node) != gpu_per_node or not _node_is_schedulable(node):
                    continue

                tasks, declared_count = _node_task_associations(node)
                if declared_count == 0 and _node_gpu_used(node) == 0:
                    full_free_nodes += 1
                    continue

                task_ids = tuple(str(task.get("id") or "").strip() for task in tasks)
                if (
                    low_task_ids
                    and declared_count == len(tasks)
                    and task_ids
                    and all(task_id and task_id in low_task_ids for task_id in task_ids)
                ):
                    reclaimable_nodes += 1

            results.append(
                FullFreeNodeCount(
                    group_id=gid,
                    group_name=group_name,
                    gpu_per_node=gpu_per_node,
                    total_nodes=total_nodes,
                    ready_nodes=ready_nodes,
                    full_free_nodes=full_free_nodes,
                    reclaimable_nodes=reclaimable_nodes,
                )
            )

    except SessionExpiredError:
        # Already refreshed once inside request_json(); never start another.
        raise

    results.sort(
        key=lambda r: (r.high_priority_free_nodes, r.full_free_nodes),
        reverse=True,
    )
    return results


__all__ = [
    "QUOTA_PRIORITY_SPEC_FIELDS",
    "get_accurate_resource_availability",
    "get_accurate_gpu_availability",
    "get_full_free_node_counts",
    "get_quota_priority_levels",
    "list_member_usage",
    "list_node_dimension",
    "list_node_events",
    "list_node_specs",
    "list_compute_groups",
    "list_task_usage",
]
