"""Quota catalog rows, cached in the per-account resource identity index.

``POST /resource_prices/logic_compute_groups/`` answers one
``(workspace, workload, compute group)`` at a time, so listing or resolving a
quota costs one request per compute group in the workspace. The rows behind it
are catalog data -- they only move when an admin edits a compute group's specs
-- so they cache well.

A quota row is a name-to-handle mapping like any other resource: the name the
user types is the ``gpu,cpu,mem`` triple and the handle is the platform
``quota_id``. So it is stored as an ordinary ``ResourceIdentity``, with the
compute group name in ``compute_group`` and the raw price object in
``payload``. That buys the whole index for free -- TTL, tombstoning of specs
an admin removed, ``cache refresh``, ``cache status``, ``cache clear`` -- and
the resolver's "this triple exists in two compute groups" ambiguity check is
just a lookup returning two rows.

Consumers keep the plain ``(logic_compute_group_id) -> list[price]`` loader
contract; the cache sits inside the loader. The platform stays authoritative:
a stale scope, a miss, or any cache failure falls through to the live call.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional, Sequence

from inspire.cli.utils.resource_index import (
    DEFAULT_TTL_SECONDS,
    ResourceIdentity,
    ResourceIndex,
    ResourceScope,
    quota_resource_type,
    scope_for_session,
)
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import WebSession

logger = logging.getLogger(__name__)

# Workload name -> platform schedule config type.
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
    """Build the cache scope for one workspace/workload quota catalog."""
    if not str(workload or "").strip():
        return None
    try:
        return scope_for_session(
            session,
            resource_type=quota_resource_type(workload),
            workspace_id=workspace_id,
            owner_scope="self",
        )
    except Exception:  # noqa: BLE001 - the cache must never block live lookups
        logger.debug("Quota cache scope initialization failed", exc_info=True)
        return None


def quota_triple(price: dict[str, Any]) -> str:
    """Return the ``gpu,cpu,mem`` name the user types for this price row."""
    memory = (
        price.get("memory_size_gib")
        or price.get("memory_size")
        or price.get("memory_size_gb")
        or 0
    )
    try:
        memory_gib = int(memory)
    except (TypeError, ValueError):
        memory_gib = 0
    return (
        f"{int(price.get('gpu_count') or 0)},"
        f"{int(price.get('cpu_count') or 0)},"
        f"{memory_gib}"
    )


def _quota_handle(price: dict[str, Any], *, logic_compute_group_id: str) -> str:
    """Return the row's stable handle within its scope.

    ``quota_id`` is the platform handle and the value ``create`` echoes back.
    A price row without one cannot be created against, but it still shows up
    in ``<workload> quota``, so give it a deterministic synthetic key rather
    than dropping it from the cache.
    """
    quota_id = str(price.get("quota_id") or price.get("spec_id") or "").strip()
    if quota_id:
        return quota_id
    return f"{logic_compute_group_id}:{quota_triple(price)}"


def quota_records(
    prices: Sequence[dict[str, Any]],
    *,
    logic_compute_group_id: str,
    compute_group_name: str,
) -> list[ResourceIdentity]:
    """Project one group's price rows onto cacheable identity records."""
    records: list[ResourceIdentity] = []
    for price in prices:
        if not isinstance(price, dict):
            continue
        try:
            payload = json.dumps(price, ensure_ascii=False)
        except (TypeError, ValueError):
            # A row that will not round-trip through JSON must not poison the
            # cache; leave it to the live path.
            continue
        records.append(
            ResourceIdentity(
                resource_id=_quota_handle(
                    price, logic_compute_group_id=logic_compute_group_id
                ),
                name=quota_triple(price),
                owner_id=logic_compute_group_id,
                compute_group=compute_group_name,
                payload=payload,
            )
        )
    return records


def prices_from_records(records: Sequence[ResourceIdentity]) -> list[dict[str, Any]]:
    """Rebuild raw price rows from cached identity records."""
    prices: list[dict[str, Any]] = []
    for record in records:
        if not record.payload:
            continue
        try:
            price = json.loads(record.payload)
        except (TypeError, ValueError):
            continue
        if isinstance(price, dict):
            prices.append(price)
    return prices


def _group_id(group: dict) -> str:
    return str(group.get("logic_compute_group_id") or group.get("id") or "").strip()


def _group_name(group: dict) -> str:
    return str(group.get("name") or group.get("logic_compute_group_name") or "").strip()


def fetch_quota_catalog(
    session: object,
    *,
    workspace_id: str,
    workload: str,
    groups: Optional[Sequence[dict]] = None,
) -> list[ResourceIdentity]:
    """Fetch one workspace's complete quota catalog for one workload."""
    schedule_config_type = SCHEDULE_TYPE_BY_WORKLOAD[workload]
    if groups is None:
        groups = browser_api_module.list_notebook_compute_groups(
            workspace_id=workspace_id,
            session=session,  # type: ignore[arg-type]
        )
    records: list[ResourceIdentity] = []
    for group in groups or []:
        group_id = _group_id(group)
        if not group_id:
            continue
        prices = browser_api_module.get_resource_prices(
            workspace_id=workspace_id,
            logic_compute_group_id=group_id,
            schedule_config_type=schedule_config_type,
            session=session,  # type: ignore[arg-type]
        )
        records.extend(
            quota_records(
                prices or [],
                logic_compute_group_id=group_id,
                compute_group_name=_group_name(group),
            )
        )
    return records


class CachedPricesLoader:
    """A ``prices_loader`` that reads through the quota catalog cache.

    The scope is the workspace's whole catalog for one workload, so the first
    miss fetches every compute group and reconciles the scope; from then on a
    lookup answers locally, and a group with no rows is authoritatively empty
    rather than merely unfetched.

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
    ) -> None:
        self._session = session
        self._workspace_id = workspace_id
        self._schedule_config_type = schedule_config_type
        self._workload = workload_for_schedule_type(schedule_config_type)
        self.served_from_cache: set[str] = set()
        self._scope: ResourceScope | None = None
        self._index: ResourceIndex | None = None
        self._cached_by_group: dict[str, list[dict[str, Any]]] | None = None
        if self._workload not in SCHEDULE_TYPE_BY_WORKLOAD:
            return
        self._scope = quota_scope_for_session(
            session,
            workspace_id=workspace_id,
            workload=self._workload,
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

    def _load_cached_catalog(self) -> dict[str, list[dict[str, Any]]] | None:
        """Return the whole cached catalog when the scope is fresh and complete."""
        if self._cached_by_group is not None:
            return self._cached_by_group
        if self._index is None or self._scope is None:
            return None
        try:
            due = self._index.scope_due(
                self._scope,
                interval_seconds=DEFAULT_TTL_SECONDS[
                    quota_resource_type(self._workload)
                ],
                require_full=True,
            )
            if due:
                return None
            records = self._index.list_identities(self._scope)
        except Exception:  # noqa: BLE001 - a disposable cache never fails a command
            logger.debug("Quota cache lookup failed", exc_info=True)
            return None
        by_group: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            by_group.setdefault(record.owner_id, []).extend(
                prices_from_records([record])
            )
        self._cached_by_group = by_group
        return by_group

    def __call__(self, logic_compute_group_id: str) -> list[dict]:
        group_id = str(logic_compute_group_id or "").strip()
        cached = self._load_cached_catalog()
        if cached is not None:
            self.served_from_cache.add(group_id)
            return list(cached.get(group_id, []))

        prices = browser_api_module.get_resource_prices(
            workspace_id=self._workspace_id,
            logic_compute_group_id=group_id,
            schedule_config_type=self._schedule_config_type,
            session=self._session,
        )
        return list(prices)


__all__ = [
    "CachedPricesLoader",
    "SCHEDULE_TYPE_BY_WORKLOAD",
    "WORKLOAD_BY_SCHEDULE_TYPE",
    "fetch_quota_catalog",
    "prices_from_records",
    "quota_records",
    "quota_scope_for_session",
    "quota_triple",
    "workload_for_schedule_type",
]
