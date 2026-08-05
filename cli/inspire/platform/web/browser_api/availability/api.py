"""Browser (web-session) APIs for compute group availability (HTTP endpoints)."""

from __future__ import annotations

from typing import Optional

from .models import FullFreeNodeCount, GPUAvailability
from inspire.platform.web.browser_api.core import _browser_api_path, _get_base_url, _request_json
from inspire.platform.web.session import SessionExpiredError, WebSession, clear_session_cache, get_web_session


def list_compute_groups(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List compute groups using the browser API."""
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        raise ValueError("Workspace selection is required.")

    body = {
        "page_size": -1,
        "page_num": 1,
        "filter": {"workspace_id": workspace_id},
    }

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/logic_compute_groups/list"),
        referer=f"{_get_base_url()}/jobs/distributedTraining",
        body=body,
        timeout=30,
    )
    code = data.get("code")
    if code not in (None, 0):
        raise ValueError(f"API error: {data.get('message') or code}")
    return data.get("data", {}).get("logic_compute_groups", [])


def _group_id(group: dict) -> str:
    return str(group.get("logic_compute_group_id") or group.get("id") or "").strip()


def _group_name(group: dict) -> str:
    return str(
        group.get("name")
        or group.get("logic_compute_group_name")
        or group.get("compute_group_name")
        or ""
    ).strip()


def _iter_payload_lists(value: object):
    if isinstance(value, list):
        yield value
        for item in value:
            yield from _iter_payload_lists(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_payload_lists(item)


def _extract_compute_groups(payload: dict) -> list[dict]:
    data = payload.get("data", payload)
    preferred_keys = (
        "logic_compute_groups",
        "logic_compute_group_list",
        "compute_groups",
        "compute_group_list",
        "groups",
    )
    for key in preferred_keys:
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, list):
            groups = [item for item in value if isinstance(item, dict) and _group_id(item)]
            if groups:
                return groups

    for value in _iter_payload_lists(data):
        groups = [item for item in value if isinstance(item, dict) and _group_id(item)]
        if groups:
            return groups
    return []


def cluster_basic_info(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List compute groups from the live cluster_basic_info endpoint."""
    if session is None:
        session = get_web_session()
    if workspace_id is None:
        raise ValueError("Workspace selection is required.")

    body = {"workspace_id": workspace_id, "filter": {"workspace_id": workspace_id}}
    attempts = (
        ("POST", "/compute_resources/cluster_basic_info", body),
        ("GET", f"/compute_resources/cluster_basic_info?workspace_id={workspace_id}", None),
        ("POST", "/cluster_basic_info", body),
    )

    last_error: Exception | None = None
    for method, path, request_body in attempts:
        try:
            payload = _request_json(
                session,
                method,
                _browser_api_path(path),
                referer=f"{_get_base_url()}/jobs/distributedTraining",
                body=request_body,
                timeout=30,
            )
            groups = _extract_compute_groups(payload)
            if groups:
                return groups
        except SessionExpiredError:
            raise
        except Exception as exc:
            last_error = exc
            continue

    if last_error is not None:
        raise ValueError(f"cluster_basic_info unavailable: {last_error}") from last_error
    raise ValueError("cluster_basic_info returned no compute groups")


def _extract_node_dimensions(payload: dict) -> list[dict]:
    data = payload.get("data", payload)
    for key in ("node_dimensions", "node_dimension", "nodes", "items", "list"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    for value in _iter_payload_lists(data):
        nodes = [item for item in value if isinstance(item, dict)]
        if nodes and any("gpu_count" in item or "status" in item for item in nodes):
            return nodes
    return []


def list_node_dimension(
    logic_compute_group_id: str,
    *,
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List live node dimensions for one compute group."""
    if session is None:
        session = get_web_session()
    if workspace_id is None:
        raise ValueError("Workspace selection is required.")

    body = {
        "workspace_id": workspace_id,
        "logic_compute_group_id": logic_compute_group_id,
        "filter": {"logic_compute_group_id": logic_compute_group_id},
        "page_num": 1,
        "page_size": -1,
    }
    attempts = (
        ("POST", "/compute_resources/list_node_dimension", body),
        ("POST", "/compute_resources/node_dimension/list", body),
        (
            "GET",
            f"/compute_resources/node_specs/logic_compute_groups/{logic_compute_group_id}",
            None,
        ),
    )

    last_error: Exception | None = None
    for method, path, request_body in attempts:
        try:
            payload = _request_json(
                session,
                method,
                _browser_api_path(path),
                referer=f"{_get_base_url()}/jobs/distributedTraining",
                body=request_body,
                timeout=30,
            )
            nodes = _extract_node_dimensions(payload)
            if nodes:
                return nodes
        except SessionExpiredError:
            raise
        except Exception as exc:
            last_error = exc
            continue

    if last_error is not None:
        raise ValueError(f"list_node_dimension unavailable: {last_error}") from last_error
    return []


def _list_live_compute_groups(
    *,
    workspace_id: str,
    session: WebSession,
) -> list[dict]:
    try:
        return cluster_basic_info(workspace_id=workspace_id, session=session)
    except ValueError:
        return list_compute_groups(workspace_id=workspace_id, session=session)


def _compute_node_summary(nodes: list[dict]) -> dict[str, int]:
    total_nodes = 0
    ready_nodes = 0
    free_nodes = 0
    gpu_per_node = 0

    for node in nodes:
        gpu_count = int(node.get("gpu_count") or node.get("gpu_total") or 0)
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
                    data = _request_json(
                        session,
                        "GET",
                        _browser_api_path(f"/compute_resources/logic_compute_groups/{group_id}"),
                        referer=f"{_get_base_url()}/jobs/distributedTraining",
                        timeout=30,
                    )
                except SessionExpiredError:
                    raise
                except ValueError:
                    continue

                try:
                    node_summary = _compute_node_summary(
                        list_node_dimension(group_id, workspace_id=wid, session=session)
                    )
                except SessionExpiredError:
                    raise
                except ValueError:
                    node_summary = {
                        "total_nodes": 0,
                        "ready_nodes": 0,
                        "free_nodes": 0,
                        "gpu_per_node": 0,
                    }

                resources = data.get("data", {}).get("logic_resouces", {})
                gpu_stats = data.get("data", {}).get("gpu_type_stats", [{}])

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
    session: Optional[WebSession] = None,
    _retry: bool = True,
) -> list[FullFreeNodeCount]:
    """Get per-group counts of fully-free nodes using the browser API."""
    if session is None:
        session = get_web_session()

    results: list[FullFreeNodeCount] = []

    try:
        for gid in group_ids:
            body = {
                "page_num": 1,
                "page_size": -1,
                "filter": {"logic_compute_group_id": gid},
            }

            payload = _request_json(
                session,
                "POST",
                _browser_api_path("/cluster_nodes/list"),
                referer=f"{_get_base_url()}/jobs/distributedTraining",
                body=body,
                timeout=30,
            )

            if payload.get("code") != 0:
                raise ValueError(f"API error: {payload.get('message')}")

            data = payload.get("data", {})
            nodes = data.get("nodes", []) or []

            total_nodes = len(nodes)
            ready_nodes = 0
            full_free_nodes = 0
            group_name = ""

            for node in nodes:
                if not group_name:
                    group_name = node.get("logic_compute_group_name", "") or ""

                status = (node.get("status") or "").upper()
                if status == "READY":
                    ready_nodes += 1

                    node_gpu = node.get("gpu_count", 0) or 0
                    task_list = node.get("task_list") or []
                    if node_gpu == gpu_per_node and len(task_list) == 0:
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
    "cluster_basic_info",
    "list_node_dimension",
    "list_compute_groups",
]
