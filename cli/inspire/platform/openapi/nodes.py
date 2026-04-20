"""Node-related helpers for the Inspire OpenAPI client."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from inspire.platform.openapi.errors import InspireAPIError, ValidationError

logger = logging.getLogger(__name__)


def list_cluster_nodes(
    api,  # noqa: ANN001
    *,
    page_num: int = 1,
    page_size: int = 10,
    resource_pool: Optional[str] = None,
) -> Dict[str, Any]:
    """Get cluster node list."""
    api._check_authentication()

    if page_num < 1:
        raise ValidationError("Page number must be at least 1")
    if page_size < 1 or page_size > 1000:
        raise ValidationError("Page size must be between 1 and 1000")

    valid_pools = ["online", "backup", "fault", "unknown"]
    if resource_pool and resource_pool not in valid_pools:
        raise ValidationError(f"Resource pool must be one of: {valid_pools}")

    payload: Dict[str, Any] = {"page_num": page_num, "page_size": page_size}

    if resource_pool:
        payload["filter"] = {"resource_pool": resource_pool}

    result = api._make_request("POST", api.endpoints.CLUSTER_NODES_LIST, payload)

    if result.get("code") == 0:
        node_count = len(result["data"].get("nodes", []))
        logger.info("🖥️  Retrieved %s nodes successfully.", node_count)
        return result

    error_msg = result.get("message", "Unknown error")
    raise InspireAPIError(f"Failed to get node list: {error_msg}")


__all__ = ["list_cluster_nodes"]
