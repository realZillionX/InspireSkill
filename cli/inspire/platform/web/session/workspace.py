"""Workspace availability helpers using a WebSession."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .models import WebSession


def fetch_node_specs(
    session: WebSession,
    compute_group_id: str,
    *,
    request_json_fn: Callable[..., dict],
    base_url: str = "https://api.example.com",
) -> dict:
    """Fetch detailed node specs for a compute group using web session.

    This API returns per-GPU task information via node_dimensions.

    Note: This endpoint is a web UI internal API that requires Keycloak
    authentication. We use the captured browser cookies for HTTP requests.
    """
    if not session.storage_state or not session.storage_state.get("cookies"):
        if not session.cookies:
            raise ValueError("Session expired or invalid (missing storage state)")

    url = f"{base_url}/api/v1/compute_resources/node_specs/logic_compute_groups/{compute_group_id}"
    return request_json_fn(
        session,
        "GET",
        url,
        headers={"Referer": f"{base_url}/jobs/distributedTraining"},
        timeout=30,
    )


def fetch_workspace_availability(
    session: WebSession,
    *,
    request_json_fn: Callable[..., dict],
    base_url: str = "https://api.example.com",
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> list[dict]:
    """Fetch workspace-specific GPU availability.

    Uses the browser API endpoint POST /api/v1/cluster_nodes/list which returns
    nodes with complete task_list data for accurate free node counting.
    This matches what the user sees in the browser.

    Args:
        session: Web session with storage_state and workspace_id
        request_json_fn: Callable used to issue authenticated requests
        base_url: Base URL for the API
        progress_callback: Optional callback(fetched, total) for progress updates

    Returns:
        List of node dictionaries with availability info.

    Raises:
        ValueError: If session is invalid or workspace_id is missing.
    """
    _ = progress_callback  # currently unused (retained for API compatibility)
    if not session.storage_state or not session.storage_state.get("cookies"):
        if not session.cookies:
            raise ValueError("Session expired or invalid (missing storage state)")

    if not session.workspace_id:
        raise ValueError("No workspace_id in session. Please login again.")

    url = f"{base_url}/api/v1/cluster_nodes/list"
    body = {
        "page_num": 1,
        "page_size": -1,  # Get all nodes
        "filter": {},  # No filter to get all workspace nodes
    }

    data = request_json_fn(
        session,
        "POST",
        url,
        body=body,
        headers={"Referer": f"{base_url}/jobs/distributedTraining"},
        timeout=30,
    )
    return data.get("data", {}).get("nodes", [])


@dataclass
class GPUAvailability:
    """Per-GPU availability for a compute group."""

    group_id: str
    group_name: str
    gpu_type: str
    total_gpus: int
    free_gpus: int
    low_priority_gpus: int  # GPUs used by low-priority tasks


def fetch_gpu_availability(
    session: WebSession,
    compute_group_ids: list[str],
    *,
    request_json_fn: Callable[..., dict],
    base_url: str = "https://api.example.com",
) -> list[GPUAvailability]:
    """Fetch accurate per-GPU availability for compute groups."""
    results = []

    for group_id in compute_group_ids:
        try:
            data = fetch_node_specs(
                session,
                group_id,
                request_json_fn=request_json_fn,
                base_url=base_url,
            )

            # Parse the response to count free GPUs
            nodes = data.get("data", {}).get("node_dimensions", [])

            total_gpus = 0
            free_gpus = 0
            low_priority_gpus = 0
            group_name = ""
            gpu_type = ""

            for node in nodes:
                gpu_count = node.get("gpu_count", 8)
                total_gpus += gpu_count

                # Check tasks_associated for each GPU dimension
                tasks = node.get("tasks_associated", [])
                if not tasks:
                    free_gpus += gpu_count
                else:
                    # Check if tasks are low priority
                    for task in tasks:
                        priority = task.get("priority", 10)
                        if priority < 5:  # Low priority threshold
                            low_priority_gpus += 1

                if not group_name:
                    group_name = node.get("logic_compute_group_name", "Unknown")
                if not gpu_type:
                    gpu_info = node.get("gpu_info", {})
                    gpu_type = gpu_info.get("gpu_type_display", "Unknown")

            results.append(
                GPUAvailability(
                    group_id=group_id,
                    group_name=group_name,
                    gpu_type=gpu_type,
                    total_gpus=total_gpus,
                    free_gpus=free_gpus,
                    low_priority_gpus=low_priority_gpus,
                )
            )

        except Exception as e:
            # Skip groups that fail
            print(f"Warning: Failed to fetch {group_id}: {e}")
            continue

    return results
