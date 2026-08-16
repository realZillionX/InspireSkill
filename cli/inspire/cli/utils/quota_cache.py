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

One request per compute group is also one chance per compute group to be rate
limited, and the whole point of this cache is that a group with no rows means
"this group has no quotas for this workload". Those two facts collide unless a
fan-out that failed anywhere is recorded as incomplete -- which is what
:class:`QuotaCatalog` carries and why only a complete catalog is allowed to
reconcile a scope.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
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

# Workload name -> the job types a compute group must advertise to run it.
# Every group carries its own `support_job_type_list`, and the support is
# genuinely uneven: in `CPU资源空间` only two of the four groups take `ray_job`,
# and only one takes serving. Quoting a group that cannot run the workload
# sends the user all the way to `已选择的计算类型组不支持此类型任务` at create time.
GROUP_JOB_TYPES_BY_WORKLOAD: dict[str, frozenset[str]] = {
    "notebook": frozenset({"interactive_modeling"}),
    "job": frozenset({"distributed_training"}),
    "hpc": frozenset({"hpc_job"}),
    "ray": frozenset({"ray_job"}),
    "serving": frozenset({"inference_serving_customize", "inference_serving_exclusive"}),
    "tensorboard": frozenset({"tensorboard"}),
}


def _declared_job_types(group: dict) -> list[str]:
    """Read `support_job_type_list`, which arrives JSON-encoded as a string.

    The platform sends `'["interactive_modeling","hpc_job"]'`, not a real
    array, so an isinstance check against list silently reads every group as
    undeclared. Both shapes are accepted here in case that ever changes.
    """
    declared = group.get("support_job_type_list")
    if isinstance(declared, str):
        try:
            declared = json.loads(declared)
        except (TypeError, ValueError):
            return []
    if not isinstance(declared, list):
        return []
    return [str(entry).strip() for entry in declared if str(entry).strip()]


def group_supports_workload(group: dict, workload: str) -> bool:
    """Whether a compute group advertises support for *workload*.

    A group that does not declare `support_job_type_list` is kept: the absence
    is our ignorance, not the platform's refusal, and hiding a usable group is
    the worse failure of the two — it reads as "this workspace cannot run this",
    which no error message would ever correct.
    """
    wanted = GROUP_JOB_TYPES_BY_WORKLOAD.get(workload)
    if not wanted:
        return True
    declared = _declared_job_types(group)
    if not declared:
        return True
    return any(entry in wanted for entry in declared)


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
    """Return the row's cache key, which must be unique per compute group.

    **``quota_id`` alone is not unique.** The platform reuses one spec id
    across every group that offers that shape — measured on 分布式训练空间:
    9 groups, 11 distinct ``quota_id`` values, 7 of them shared by 4 to 7
    groups each. The cache's primary key does not include ``owner_id``, so a
    bare ``quota_id`` made each group overwrite the previous one's row and the
    stored catalog collapsed to one entry per spec — 11 rows across 3 groups
    instead of 32 across 8. Groups vanished from ``<workload> quota`` and
    became unreachable by ``--group``, while the platform was answering for
    all of them.

    The group id therefore always leads. The real ``quota_id`` the create call
    echoes back is read from the row's ``payload``, never from this key.
    """
    quota_id = str(price.get("quota_id") or price.get("spec_id") or "").strip()
    return f"{logic_compute_group_id}:{quota_id or quota_triple(price)}"


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


@dataclass(frozen=True)
class QuotaCatalog:
    """One workspace/workload catalog, and whether it is the whole of it.

    ``complete`` is the only thing standing between a refresh and a cache that
    claims a workspace has no quotas because the platform was rate-limiting
    the fan-out. It is true only when every compute group answered.
    """

    records: list[ResourceIdentity]
    complete: bool = True
    error: str = ""


def fetch_quota_catalog(
    session: object,
    *,
    workspace_id: str,
    workload: str,
    groups: Optional[Sequence[dict]] = None,
) -> QuotaCatalog:
    """Fetch one workspace's quota catalog for one workload.

    The catalog is a fan-out of one request per compute group, and a fan-out
    fails in pieces. A group that could not be read leaves the catalog
    incomplete and the rest of the groups still cached: the caller merges
    what came back instead of reconciling a scope it never fully saw.

    Failing to list the compute groups at all is different -- there is no
    catalog, partial or otherwise -- so that one propagates.
    """
    schedule_config_type = SCHEDULE_TYPE_BY_WORKLOAD[workload]
    if groups is None:
        groups = browser_api_module.list_notebook_compute_groups(
            workspace_id=workspace_id,
            session=session,  # type: ignore[arg-type]
            allow_config_fallback=False,
        )
    records: list[ResourceIdentity] = []
    failures: list[str] = []
    for group in groups or []:
        group_id = _group_id(group)
        if not group_id:
            continue
        if not group_supports_workload(group, workload):
            continue
        try:
            prices = browser_api_module.get_resource_prices(
                workspace_id=workspace_id,
                logic_compute_group_id=group_id,
                schedule_config_type=schedule_config_type,
                session=session,  # type: ignore[arg-type]
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # noqa: BLE001 - one unreadable group, not a dead refresh
            logger.debug(
                "Quota catalog fetch failed for one compute group", exc_info=True
            )
            failures.append(
                f"{_group_name(group) or 'compute group'}: "
                f"{str(exc) or type(exc).__name__}"
            )
            continue
        records.extend(
            quota_records(
                prices or [],
                logic_compute_group_id=group_id,
                compute_group_name=_group_name(group),
            )
        )
    if failures:
        return QuotaCatalog(
            records,
            complete=False,
            error=(
                f"{len(failures)} compute group(s) did not answer; "
                f"kept the previously cached rows. First: {failures[0]}"
            ),
        )
    return QuotaCatalog(records)


class CachedPricesLoader:
    """A ``prices_loader`` that reads through the quota catalog cache.

    The scope is the workspace's whole catalog for one workload, so the first
    miss fetches every compute group and reconciles the scope; from then on a
    lookup answers locally, and a group with no rows is authoritatively empty
    rather than merely unfetched. That authority rests on the scope having had
    a *complete* refresh: a partial one leaves the scope short of full, and
    the loader goes back to the platform per group.

    ``served_from_cache`` records which compute groups were answered locally.
    Callers that treat an empty live response as a stale-handle signal consult
    it, because an empty *cached* response is an authoritative "this group has
    no quotas for this workload", not evidence of a dead group handle.

    That authority is per group and stops there. A scope holding nothing for
    *any* group is read as a miss and refetched -- see `_load_cached_catalog`
    for why the one workspace that really has no quotas pays for it.
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
        if not by_group:
            # A scope marked complete that holds nothing for *any* group is a
            # cache accident, not a workspace without quotas, and answering
            # from it is the worst failure this cache can produce: every
            # `<workload> quota` prints "No quota rows found." and every
            # `create` refuses a `--quota` the platform would have accepted.
            # It has happened -- 150 rows left unreadable by a scope-keying
            # change, with the scope still flagged fully refreshed.
            #
            # A genuinely quota-less workspace pays a per-group fetch for this.
            # That is the right side to be wrong on: the cost is N requests,
            # and the alternative cost is a CLI that cannot create anything.
            return None
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
    "QuotaCatalog",
    "SCHEDULE_TYPE_BY_WORKLOAD",
    "WORKLOAD_BY_SCHEDULE_TYPE",
    "fetch_quota_catalog",
    "prices_from_records",
    "quota_records",
    "quota_scope_for_session",
    "quota_triple",
    "workload_for_schedule_type",
]
