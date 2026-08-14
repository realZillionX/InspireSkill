"""Browser (web-session) APIs for compute group availability (HTTP endpoints)."""

from __future__ import annotations

from typing import Optional

from .models import FullFreeNodeCount, GPUAvailability
from inspire.platform.web.browser_api.core import (
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
    "get_accurate_resource_availability",
    "get_accurate_gpu_availability",
    "get_full_free_node_counts",
    "list_node_dimension",
    "list_compute_groups",
]
