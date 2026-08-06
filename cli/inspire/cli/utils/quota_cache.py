"""Read-through cache for the workspace quota catalog.

``POST /resource_prices/logic_compute_groups/`` answers one
``(workspace, workload, compute group)`` at a time, so listing or resolving a
quota costs one request per compute group in the workspace. The rows behind it
are catalog data -- they only move when an admin edits a compute group's specs
-- so they cache well.

Consumers keep the plain ``(logic_compute_group_id) -> list[price]`` loader
contract; the cache sits inside the loader. The platform stays authoritative:
a miss, an expired entry, or any cache failure falls through to the live call.
"""

from __future__ import annotations

import logging
from typing import Optional

from inspire.cli.utils.resource_index import (
    QUOTA_RESOURCE_TYPE,
    ResourceIndex,
    ResourceScope,
    scope_for_session,
)
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import WebSession

logger = logging.getLogger(__name__)

# Workload name -> platform schedule config type. The workload name is also the
# cache partition key, so `notebook` and `hpc` quotas for one compute group
# never overwrite each other.
SCHEDULE_TYPE_BY_WORKLOAD: dict[str, str] = {
    "notebook": "SCHEDULE_CONFIG_TYPE_DSW",
    "job": "SCHEDULE_CONFIG_TYPE_TRAIN",
    "hpc": "SCHEDULE_CONFIG_TYPE_HPC",
    "ray": "SCHEDULE_CONFIG_TYPE_RAY_JOB",
    "serving": "SCHEDULE_CONFIG_TYPE_SERVE",
}

WORKLOAD_BY_SCHEDULE_TYPE: dict[str, str] = {
    schedule_type: workload
    for workload, schedule_type in SCHEDULE_TYPE_BY_WORKLOAD.items()
}


def workload_for_schedule_type(schedule_config_type: str) -> str:
    """Return the cache partition name for a platform schedule config type."""
    return WORKLOAD_BY_SCHEDULE_TYPE.get(
        str(schedule_config_type or "").strip(),
        str(schedule_config_type or "").strip(),
    )


def quota_scope_for_session(
    session: object,
    *,
    workspace_id: str,
    workload: str,
) -> ResourceScope | None:
    """Build the cache scope for one workspace/workload quota catalog slice."""
    if not str(workload or "").strip():
        return None
    try:
        return scope_for_session(
            session,
            resource_type=QUOTA_RESOURCE_TYPE,
            workspace_id=workspace_id,
            owner_scope=str(workload).strip(),
        )
    except Exception:  # noqa: BLE001 - the cache must never block live lookups
        logger.debug("Quota cache scope initialization failed", exc_info=True)
        return None


class CachedPricesLoader:
    """A ``prices_loader`` that reads through the local quota cache.

    ``served_from_cache`` records which compute groups were answered locally.
    Callers that treat an empty live response as a stale-handle signal consult
    it, because an empty *cached* response is an authoritative "this group has
    no quotas for this workload", not evidence of a dead group handle.
    """

    def __init__(
        self,
        *,
        session: WebSession,
        workspace_id: str,
        schedule_config_type: str,
        cache_index: Optional[ResourceIndex] = None,
        use_cache: bool = True,
    ) -> None:
        self._session = session
        self._workspace_id = workspace_id
        self._schedule_config_type = schedule_config_type
        self.served_from_cache: set[str] = set()
        self._scope: ResourceScope | None = None
        self._index: ResourceIndex | None = None
        if not use_cache:
            return
        self._scope = quota_scope_for_session(
            session,
            workspace_id=workspace_id,
            workload=workload_for_schedule_type(schedule_config_type),
        )
        if self._scope is None:
            return
        if cache_index is not None:
            self._index = cache_index
            return
        try:
            self._index = ResourceIndex.for_account()
        except Exception:  # noqa: BLE001 - live lookups must still work
            logger.debug("Quota cache initialization failed", exc_info=True)

    def __call__(self, logic_compute_group_id: str) -> list[dict]:
        group_id = str(logic_compute_group_id or "").strip()
        if self._index is not None and self._scope is not None and group_id:
            try:
                cached = self._index.lookup_quota_prices(self._scope, group_id)
            except Exception:  # noqa: BLE001 - a disposable cache never fails a command
                logger.debug("Quota cache lookup failed", exc_info=True)
                cached = None
            if cached is not None:
                self.served_from_cache.add(group_id)
                return cached

        prices = browser_api_module.get_resource_prices(
            workspace_id=self._workspace_id,
            logic_compute_group_id=group_id,
            schedule_config_type=self._schedule_config_type,
            session=self._session,
        )
        if self._index is not None and self._scope is not None and group_id:
            try:
                self._index.store_quota_prices(self._scope, group_id, prices)
            except Exception:  # noqa: BLE001 - caching is best effort
                logger.debug("Quota cache write failed", exc_info=True)
        return list(prices)


__all__ = [
    "CachedPricesLoader",
    "SCHEDULE_TYPE_BY_WORKLOAD",
    "WORKLOAD_BY_SCHEDULE_TYPE",
    "quota_scope_for_session",
    "workload_for_schedule_type",
]
