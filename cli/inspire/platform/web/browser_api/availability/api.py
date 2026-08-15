"""Browser (web-session) APIs for compute group availability (HTTP endpoints)."""

from __future__ import annotations

import json
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
    TransientAPIError,
    WebSession,
    clear_session_cache,
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
    return groups if isinstance(groups, list) else []


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
    element's ``id`` is the handle the v1 price rows spell ``quota_id`` — that
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
    The v1 endpoints this replaces were either 404 or admin-only.
    """
    if session is None:
        session = get_web_session()
    if workspace_id is None:
        raise ValueError("Workspace selection is required.")

    nodes: list[dict] = []
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
        if not isinstance(rows, list) or not rows:
            break
        nodes.extend(row for row in rows if isinstance(row, dict))

        try:
            total = int(str(payload.get("total")))
        except (TypeError, ValueError):
            break
        if len(nodes) >= total or len(rows) < page_size:
            break
        page += 1
        if page > 100:  # safety cap
            break

    return nodes


_DIMENSION_PAGE_SIZE = 500
_DIMENSION_PAGE_CAP = 40


def _list_dimension_rows(
    action: str,
    list_key: str,
    *,
    workspace_id: str,
    session: WebSession,
    logic_compute_group_id: Optional[str] = None,
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
        if not isinstance(batch, list) or not batch:
            break
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
        if len(rows) >= total or len(batch) < page_size:
            break
        page += 1
        if page > _DIMENSION_PAGE_CAP:
            break

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
            )
        )
    return usages


def list_member_usage(
    workspace_id: str,
    *,
    session: Optional[WebSession] = None,
) -> list[MemberUsage]:
    """List the caller's own footprint in a workspace, split by project.

    Action: ``workspace.ListUserDimension``. Despite the name it is **not** a
    per-member view of the workspace: the platform answers with the caller's
    rows only, and passing another member's ``user_id`` returns an empty list
    instead of a denial. Use :func:`list_task_usage` for everyone's usage.

    It is worth keeping anyway because it is one pre-aggregated request, where
    the same answer from the task dimension costs a full paged sweep of every
    workload in the workspace.
    """
    if session is None:
        session = get_web_session()
    if not workspace_id:
        raise ValueError("Workspace selection is required.")

    rows = _list_dimension_rows(
        "ListUserDimension",
        "user_dimensions",
        workspace_id=workspace_id,
        session=session,
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
        return []

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


def _compute_node_summary(nodes: list[dict]) -> dict[str, int]:
    total_nodes = 0
    ready_nodes = 0
    free_nodes = 0
    gpu_per_node = 0

    for node in nodes:
        gpu_count = _node_gpu_total(node)
        if gpu_count <= 0:
            continue
        total_nodes += 1
        if gpu_per_node == 0:
            gpu_per_node = gpu_count

        status = str(node.get("status") or "").upper()
        if status == "READY":
            ready_nodes += 1

        task_list = node.get("task_list")
        tasks_associated = node.get("tasks_associated")
        has_tasks = bool(task_list or tasks_associated)
        cordon_type = str(node.get("cordon_type") or "").strip()
        is_maint = bool(node.get("is_maint", False))
        resource_pool = str(node.get("resource_pool") or "").lower()
        if (
            status == "READY"
            and not has_tasks
            and not cordon_type
            and not is_maint
            and resource_pool != "fault"
        ):
            free_nodes += 1

    return {
        "total_nodes": total_nodes,
        "ready_nodes": ready_nodes,
        "free_nodes": free_nodes,
        "gpu_per_node": gpu_per_node,
    }


def _resolve_workspace_targets(
    session: WebSession,
    workspace_id: Optional[str],
    *,
    all_workspaces: bool,
) -> list[str]:
    if workspace_id:
        return [workspace_id]

    if all_workspaces and session.all_workspace_ids:
        seen: set[str] = set()
        ordered: list[str] = []
        for wid in session.all_workspace_ids:
            if wid and wid not in seen:
                seen.add(wid)
                ordered.append(wid)
        if ordered:
            return ordered

    raise ValueError("Workspace selection is required unless all workspaces are selected.")


def get_accurate_resource_availability(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
    *,
    include_cpu: bool = False,
    all_workspaces: bool = False,
    _retry: bool = True,
) -> list[GPUAvailability]:
    """Get accurate compute-group availability, optionally including CPU-only groups."""
    if session is None:
        session = get_web_session()

    workspace_ids = _resolve_workspace_targets(
        session,
        workspace_id,
        all_workspaces=all_workspaces,
    )
    workspace_names = session.all_workspace_names or {}

    try:
        results: list[GPUAvailability] = []
        for wid in workspace_ids:
            groups = _list_live_compute_groups(workspace_id=wid, session=session)
            workspace_name = workspace_names.get(wid, "")

            for group in groups:
                group_id = _group_id(group)
                if not group_id:
                    continue
                group_name = _group_name(group)

                try:
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
                except (SessionExpiredError, TransientAPIError):
                    # Dropping the group here would under-report capacity as
                    # fact. Availability is a live answer or it is an error.
                    raise
                except ValueError:
                    continue

                try:
                    node_summary = _compute_node_summary(
                        list_node_dimension(group_id, workspace_id=wid, session=session)
                    )
                except (SessionExpiredError, TransientAPIError):
                    # Zeroed node counts read as "nothing free". Never say that
                    # because the platform was busy.
                    raise
                except ValueError:
                    node_summary = {
                        "total_nodes": 0,
                        "ready_nodes": 0,
                        "free_nodes": 0,
                        "gpu_per_node": 0,
                    }

                # Platform spelling: `logic_resouces`, not `logic_resources`.
                resources = group_resource.get("logic_resouces", {})
                gpu_stats = group_resource.get("gpu_type_stats", [{}])

                gpu_type = ""
                if gpu_stats:
                    gpu_type = gpu_stats[0].get("gpu_info", {}).get("gpu_type_display", "Unknown")

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

                resource_kind = "gpu" if gpu_total > 0 else "cpu"
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
                    )
                )

        return results

    except SessionExpiredError:
        if _retry:
            clear_session_cache()
            return get_accurate_resource_availability(
                workspace_id=workspace_id,
                session=None,
                include_cpu=include_cpu,
                all_workspaces=all_workspaces,
                _retry=False,
            )
        raise


def get_accurate_gpu_availability(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
    _retry: bool = True,
) -> list[GPUAvailability]:
    """Get accurate GPU availability for all compute groups."""
    results = get_accurate_resource_availability(
        workspace_id=workspace_id,
        session=session,
        include_cpu=False,
        all_workspaces=False,
        _retry=_retry,
    )
    return [row for row in results if row.resource_kind == "gpu"]


def get_full_free_node_counts(
    group_ids: list[str],
    *,
    gpu_per_node: int = 8,
    workspace_id_by_group: Optional[dict[str, str]] = None,
    session: Optional[WebSession] = None,
    _retry: bool = True,
) -> list[FullFreeNodeCount]:
    """Get per-group counts of fully-free nodes.

    Backed by ``workspace.ListNodeDimension``. The v1 endpoint this replaces,
    ``/cluster_nodes/list``, answers ``You are not the admin of any workspace``
    to ordinary members, so `inspire resources nodes` failed outright for them
    and the free-node column elsewhere silently read zero.
    """
    if session is None:
        session = get_web_session()

    by_group = dict(workspace_id_by_group or {})
    fallback_workspace = str(getattr(session, "workspace_id", "") or "").strip()
    results: list[FullFreeNodeCount] = []

    try:
        for gid in group_ids:
            workspace_id = by_group.get(gid) or fallback_workspace
            if not workspace_id:
                continue

            nodes = list_node_dimension(
                gid, workspace_id=workspace_id, session=session
            )

            total_nodes = len(nodes)
            ready_nodes = 0
            full_free_nodes = 0
            group_name = ""

            for node in nodes:
                if not group_name:
                    group_name = str(node.get("logic_compute_group_name") or "")

                status = str(node.get("status") or "").upper()
                if status != "READY":
                    continue
                ready_nodes += 1

                # A node counts as fully free only when every card is idle and
                # nothing is scheduled on it.
                if (
                    _node_gpu_total(node) == gpu_per_node
                    and not node.get("tasks_associated")
                    and not node.get("task_list")
                ):
                    full_free_nodes += 1

            results.append(
                FullFreeNodeCount(
                    group_id=gid,
                    group_name=group_name,
                    gpu_per_node=gpu_per_node,
                    total_nodes=total_nodes,
                    ready_nodes=ready_nodes,
                    full_free_nodes=full_free_nodes,
                )
            )

    except SessionExpiredError:
        if _retry:
            clear_session_cache()
            return get_full_free_node_counts(
                group_ids,
                gpu_per_node=gpu_per_node,
                workspace_id_by_group=workspace_id_by_group,
                session=None,
                _retry=False,
            )
        raise

    results.sort(key=lambda r: r.full_free_nodes, reverse=True)
    return results


__all__ = [
    "QUOTA_PRIORITY_SPEC_FIELDS",
    "get_accurate_resource_availability",
    "get_accurate_gpu_availability",
    "get_full_free_node_counts",
    "get_quota_priority_levels",
    "list_member_usage",
    "list_node_dimension",
    "list_node_specs",
    "list_compute_groups",
    "list_task_usage",
]
