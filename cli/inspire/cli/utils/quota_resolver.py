"""Resolve a ``(gpu, cpu, memory_gib)`` triple to a unique platform ``quota_id``.

Quotas on Inspire are flat rows exposed by
``POST /resource_prices/logic_compute_groups/``. Each row has a
``quota_id`` plus ``(gpu_count, cpu_count, memory_size_gib, gpu_info)``.
The user passes the triple; this module queries every compute group in
the workspace, filters for rows whose three numbers match exactly, and
demands exactly one row survives. GPU type falls out of the matched row.

When multiple compute groups in the same workspace expose the same
triple (e.g. an H100 group and an H200 group both offering
``(1, 20, 200)``), scheduling callers must pass the exact compute group
name via ``--group`` to disambiguate. Query commands may offer keyword
filters upstream, but this resolver is used by create paths.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Callable, Iterable, Optional
from inspire.services.catalog.quotas import (
    QuotaParseError, QuotaMatchError, QuotaCatalogUnavailable, QuotaSpec,
    ResolvedQuota, parse_quota, build_resource_spec_price,
)
from inspire.cli.utils.id_resolver import is_stale_handle_error
from inspire.services.catalog.quota_cache import (
    SCHEDULE_TYPE_BY_WORKLOAD,
    CachedPricesLoader,
    group_supports_workload,
    workload_for_schedule_type,
)
from inspire.services.utils.raw_ids import scrub_raw_ids
from inspire.services.catalog.resource_index import (
    ResourceIdentity,
    ResourceIndex,
    ResourceScope,
    StaleResourceIndexRefresh,
    scope_for_session,
)
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api.availability import QUOTA_PRIORITY_SPEC_FIELDS
from inspire.platform.web.session import WebSession, is_transient_api_error
from inspire.services.catalog.workload_quota import (
    workload_publishes_priority_levels as workload_publishes_priority_levels,
    allowed_priority_levels_for as allowed_priority_levels_for,
    describe_priority_levels as describe_priority_levels,
    priority_level_name as priority_level_name,
    ensure_priority_allowed as ensure_priority_allowed,
    match_quota_rows,
    format_row_catalog as format_row_catalog,
)
from inspire.services.catalog.quotas import validate_compute_group_name as validate_compute_group_name






logger = logging.getLogger(__name__)

SCHEDULE_TYPE_DSW = SCHEDULE_TYPE_BY_WORKLOAD["notebook"]
SCHEDULE_TYPE_HPC = SCHEDULE_TYPE_BY_WORKLOAD["hpc"]
SCHEDULE_TYPE_TRAIN = SCHEDULE_TYPE_BY_WORKLOAD["job"]
SCHEDULE_TYPE_SERVING = SCHEDULE_TYPE_BY_WORKLOAD["serving"]
SCHEDULE_TYPE_RAY = SCHEDULE_TYPE_BY_WORKLOAD["ray"]


def _extract_gpu_type(price: dict[str, Any]) -> str:
    gpu_info_payload = price.get("gpu_info")
    gpu_info: dict[str, Any] = gpu_info_payload if isinstance(gpu_info_payload, dict) else {}
    return str(
        gpu_info.get("gpu_type_display")
        or gpu_info.get("gpu_type")
        or gpu_info.get("brand_name")
        or price.get("gpu_type")
        or ""
    ).strip()


def _extract_memory_gib(price: dict) -> int:
    value = (
        price.get("memory_size_gib")
        or price.get("memory_size")
        or price.get("memory_size_gb")
        or 0
    )
    try:
        return int(value)
    except Exception:
        return 0


def _group_id(group: dict) -> str:
    return str(group.get("logic_compute_group_id") or group.get("id") or "").strip()


def _group_name(group: dict, fallback: str = "") -> str:
    return str(group.get("name") or group.get("logic_compute_group_name") or fallback).strip()


PricesLoader = Callable[[str], list[dict]]
GroupsLoader = Callable[[], list[dict]]
PriorityLevelsLoader = Callable[[], Optional[dict[str, tuple[str, ...]]]]

# The platform's priority vocabulary, as it spells it in
# `allowed_priority_levels`. A level outside this set is one this CLI has never
# seen and therefore cannot enforce.
PRIORITY_LEVEL_LOW = "low"
PRIORITY_LEVEL_HIGH = "high"
KNOWN_PRIORITY_LEVELS = frozenset({PRIORITY_LEVEL_LOW, PRIORITY_LEVEL_HIGH})

PRIORITY_LEVELS_UNKNOWN_DISPLAY = "unknown"
PRIORITY_LEVELS_ANY_DISPLAY = "any"


def load_quota_priority_levels(
    *,
    workspace_id: str,
    session: Optional[WebSession],
    workload: str,
) -> dict[str, tuple[str, ...]] | None:
    """Read one workspace's ``quota_id -> allowed levels`` menu for *workload*.

    ``None`` means the platform did not answer. That is not the same as "no
    restriction" and must never be rendered as one: a workspace that refuses
    HIGH on a spec looks exactly like one that allows everything, once the read
    has failed. It is also not a reason to block a create — a single unanswered
    request would then read as a quota that cannot be used at all — so the
    failure is swallowed here and carried as unknown.
    """
    spec_field = QUOTA_PRIORITY_SPEC_FIELDS.get(str(workload or "").strip(), "")
    if not spec_field or session is None:
        return None
    try:
        return browser_api_module.get_quota_priority_levels(
            workspace_id=workspace_id,
            spec_field=spec_field,
            session=session,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:  # noqa: BLE001 - an unread menu is unknown, not a failure
        logger.debug("Quota priority menu unavailable", exc_info=True)
        return None




def _default_groups_loader(
    *, workspace_id: str, session: WebSession, workload: str = ""
) -> GroupsLoader:
    def loader() -> list[dict]:
        groups = browser_api_module.list_notebook_compute_groups(
            workspace_id=workspace_id,
            session=session,
        )
        if not workload:
            return groups
        # Resolving `--quota` against a group that cannot run this workload
        # produces a match the platform then rejects at create time with
        # `已选择的计算类型组不支持此类型任务`.
        return [group for group in groups if group_supports_workload(group, workload)]

    return loader


def _default_prices_loader(
    *,
    workspace_id: str,
    session: WebSession,
    schedule_config_type: str,
    cache_index: ResourceIndex | None = None,
) -> PricesLoader:
    return CachedPricesLoader(
        session=session,
        workspace_id=workspace_id,
        schedule_config_type=schedule_config_type,
        cache_index=cache_index,
    )


def _compute_group_cache_context(
    *,
    session: WebSession | None,
    workspace_id: str,
    cache_index: ResourceIndex | None,
) -> tuple[ResourceIndex | None, ResourceScope | None]:
    if session is None:
        return None, None
    try:
        scope = scope_for_session(
            session,
            resource_type="compute-group",
            workspace_id=workspace_id,
        )
    except Exception:  # noqa: BLE001 - the cache is disposable
        logger.debug("Compute-group cache scope initialization failed", exc_info=True)
        return None, None
    if scope is None:
        return None, None
    if cache_index is not None:
        return cache_index, scope
    try:
        return ResourceIndex.for_account(), scope
    except Exception:  # noqa: BLE001 - the cache must never block live resolution
        logger.debug("Compute-group cache initialization failed", exc_info=True)
        return None, scope


def _groups_from_cache(
    *,
    index: ResourceIndex | None,
    scope: ResourceScope | None,
    name: str,
) -> list[dict]:
    if index is None or scope is None:
        return []
    try:
        return [
            {
                "logic_compute_group_id": item.resource_id,
                "name": item.name,
            }
            for item in index.lookup(scope, name, case_sensitive=False)
        ]
    except Exception:  # noqa: BLE001 - live API remains authoritative
        logger.debug("Compute-group cache lookup failed", exc_info=True)
        return []


def _cache_group_name(
    *,
    index: ResourceIndex | None,
    scope: ResourceScope | None,
    name: str,
    groups: Iterable[dict],
    full_scope: bool = False,
    expected_generation: int | None = None,
    expected_revision: int | None = None,
) -> bool:
    if index is None or scope is None:
        return True
    records = [
        ResourceIdentity(
            resource_id=_group_id(group),
            name=_group_name(group),
        )
        for group in groups
        if _group_id(group) and _group_name(group)
    ]
    try:
        if full_scope:
            index.reconcile(
                scope,
                records,
                expected_generation=expected_generation,
                expected_revision=expected_revision,
            )
        else:
            index.replace_name(
                scope,
                name,
                records,
                case_sensitive=False,
                expected_generation=expected_generation,
                expected_revision=expected_revision,
            )
        return True
    except StaleResourceIndexRefresh:
        return False
    except Exception:  # noqa: BLE001 - the cache is only an optimization
        logger.debug("Compute-group cache update failed", exc_info=True)
        return True


def _is_stale_compute_group_error(exc: BaseException) -> bool:
    # A platform that did not answer has said nothing about the handle. Re-listing
    # groups on a rate limit only spends another request on the same limiter.
    if is_transient_api_error(exc):
        return False
    if is_stale_handle_error(exc):
        return True
    for candidate in (exc, getattr(exc, "response", None)):
        for attribute in ("status_code", "status", "http_status", "code"):
            value = getattr(candidate, attribute, None)
            if value is None:
                continue
            try:
                status = int(str(value))
            except (TypeError, ValueError):
                continue
            if 100 <= status <= 599:
                if status in {401, 403} or status >= 500:
                    return False
                break
    message = str(exc).casefold()
    if any(
        marker in message
        for marker in (
            "authentication",
            "unauthorized",
            "forbidden",
            "login required",
            "token expired",
            "invalid credentials",
            "timeout",
            "timed out",
        )
    ) or any(f"{status}" in message for status in range(500, 600)):
        return False
    return any(
        marker in message
        for marker in (
            "invalid compute group",
            "unknown compute group",
            "compute group not found",
            "compute group does not exist",
            "不存在",
        )
    )


def _same_compute_group_name(left: object, right: object) -> bool:
    return str(left or "").strip().casefold() == str(right or "").strip().casefold()


def _load_price_rows(
    *,
    groups: Iterable[dict],
    prices_loader: PricesLoader,
    cached_only: bool,
) -> tuple[list[tuple[dict, dict]], bool]:
    """Collect every price row across *groups*, or say why it could not.

    A group whose prices could not be read contributes no rows, and a resolver
    that quietly accepted that would answer "your quota does not exist" using
    a catalog it never read. The one exception is the stale-handle signal a
    cached compute group handle produces, which the caller recovers from by
    re-listing groups.
    """
    rows: list[tuple[dict, dict]] = []
    saw_empty_or_stale_cached_group = False
    served_from_cache: frozenset[str] | set[str] = getattr(
        prices_loader, "served_from_cache", frozenset()
    )
    for group in groups:
        lcg_id = _group_id(group)
        if not lcg_id or not _group_name(group):
            continue
        prices: list[dict] = []
        try:
            prices = prices_loader(lcg_id)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            if cached_only and _is_stale_compute_group_error(exc):
                saw_empty_or_stale_cached_group = True
            else:
                raise QuotaCatalogUnavailable(
                    "Could not read the quota rows of compute group "
                    f"{_group_name(group)!r}: {scrub_raw_ids(exc) or type(exc).__name__}. "
                    "This is the platform failing to answer, not a workspace "
                    "without quotas -- retry, and if it persists refresh the "
                    "cached catalog with `inspire cache refresh --resource "
                    "quota-<workload> --workspace <name>`."
                ) from exc
        else:
            # An empty *live* response can mean the cached group handle died.
            # An empty *cached* response is an authoritative "no quotas for
            # this workload" and must not trigger the stale-handle retry.
            if cached_only and not prices and lcg_id not in served_from_cache:
                saw_empty_or_stale_cached_group = True
        for price in prices or []:
            rows.append((group, price))
    return rows, cached_only and saw_empty_or_stale_cached_group and not rows


def resolve_quota(
    *,
    spec: QuotaSpec,
    workspace_id: str,
    session: Optional[WebSession] = None,
    schedule_config_type: str = SCHEDULE_TYPE_DSW,
    group_override: Optional[str] = None,
    groups: Optional[Iterable[dict]] = None,
    groups_loader: Optional[GroupsLoader] = None,
    prices_loader: Optional[PricesLoader] = None,
    priority_levels_loader: Optional[PriorityLevelsLoader] = None,
    cache_index: ResourceIndex | None = None,
) -> ResolvedQuota:
    """Resolve ``spec`` to a unique ``ResolvedQuota`` in ``workspace_id``.

    ``groups`` / ``groups_loader`` / ``prices_loader`` /
    ``priority_levels_loader`` let callers inject data (used in tests and to
    share one prefetched group list between multiple calls).
    """
    target = (
        validate_compute_group_name(group_override)
        if group_override is not None
        else None
    )
    cache, cache_scope = _compute_group_cache_context(
        session=session,
        workspace_id=workspace_id,
        cache_index=cache_index,
    )
    snapshot_generation: int | None = None
    snapshot_revision: int | None = None
    cache_snapshot_available = False
    if cache is not None and cache_scope is not None:
        try:
            snapshot_generation, snapshot_revision = cache.snapshot_token(cache_scope)
            cache_snapshot_available = True
        except Exception:  # noqa: BLE001
            snapshot_generation = None
            snapshot_revision = None
    cached_only = False

    if groups is not None:
        group_list = list(groups)
    elif target is not None:
        group_list = _groups_from_cache(
            index=cache,
            scope=cache_scope,
            name=target,
        )
        if group_list:
            cached_only = True
        else:
            loader = groups_loader
            if loader is None:
                if session is None:
                    raise ValueError("resolve_quota needs a session or groups/groups_loader")
                loader = _default_groups_loader(
                    workspace_id=workspace_id,
                    session=session,
                    workload=workload_for_schedule_type(schedule_config_type),
                )
            group_list = list(loader())
            committed = _cache_group_name(
                index=cache if cache_snapshot_available else None,
                scope=cache_scope,
                name=target,
                groups=group_list,
                expected_generation=snapshot_generation,
                expected_revision=snapshot_revision,
            )
            if not committed:
                current_groups = _groups_from_cache(
                    index=cache,
                    scope=cache_scope,
                    name=target,
                )
                if current_groups:
                    group_list = current_groups
                    cached_only = True
    else:
        loader = groups_loader
        if loader is None:
            if session is None:
                raise ValueError("resolve_quota needs a session or groups/groups_loader")
            loader = _default_groups_loader(
                workspace_id=workspace_id,
                session=session,
                workload=workload_for_schedule_type(schedule_config_type),
            )
        group_list = list(loader())
        _cache_group_name(
            index=cache if cache_snapshot_available else None,
            scope=cache_scope,
            name="",
            groups=group_list,
            full_scope=True,
            expected_generation=snapshot_generation,
            expected_revision=snapshot_revision,
        )

    if target is not None:
        filtered = [
            group
            for group in group_list
            if _same_compute_group_name(_group_name(group), target)
        ]
        if not filtered:
            available = sorted({
                _group_name(g) for g in group_list if _group_name(g)
            })
            hint = ", ".join(available) if available else "(none)"
            raise QuotaMatchError(
                f"No compute group name exactly matches --group {group_override!r}. "
                "Create --group requires the full compute group name. "
                "Use a quota query --group <keyword> only to find the exact name. "
                f"Available: {hint}"
            )
        group_list = filtered

    if prices_loader is None:
        if session is None:
            raise ValueError("resolve_quota needs a session or prices_loader")
        prices_loader = _default_prices_loader(
            workspace_id=workspace_id,
            session=session,
            schedule_config_type=schedule_config_type,
            cache_index=cache_index,
        )

    all_rows, cached_group_stale = _load_price_rows(
        groups=group_list,
        prices_loader=prices_loader,
        cached_only=cached_only,
    )
    if cached_group_stale and target is not None:
        # A cached handle can outlive a deleted/recreated group. Only retry the
        # non-destructive name lookup after an empty/not-found price response;
        # network errors raised by a custom loader do not trigger blind retry.
        _cache_group_name(
            index=cache if cache_snapshot_available else None,
            scope=cache_scope,
            name=target,
            groups=[],
            expected_generation=snapshot_generation,
            expected_revision=snapshot_revision,
        )
        loader = groups_loader
        if loader is None:
            if session is None:
                raise ValueError("resolve_quota needs a session or groups/groups_loader")
            loader = _default_groups_loader(
                workspace_id=workspace_id,
                session=session,
                workload=workload_for_schedule_type(schedule_config_type),
            )
        retry_generation: int | None = None
        retry_revision: int | None = None
        retry_snapshot_available = False
        if cache is not None and cache_scope is not None:
            try:
                retry_generation, retry_revision = cache.snapshot_token(cache_scope)
                retry_snapshot_available = True
            except Exception:  # noqa: BLE001
                retry_generation = None
                retry_revision = None
        group_list = list(loader())
        committed = _cache_group_name(
            index=cache if retry_snapshot_available else None,
            scope=cache_scope,
            name=target,
            groups=group_list,
            expected_generation=retry_generation,
            expected_revision=retry_revision,
        )
        if not committed:
            current_groups = _groups_from_cache(
                index=cache,
                scope=cache_scope,
                name=target,
            )
            if current_groups:
                group_list = current_groups
        group_list = [
            group
            for group in group_list
            if _same_compute_group_name(_group_name(group), target)
        ]
        all_rows, _ = _load_price_rows(
            groups=group_list,
            prices_loader=prices_loader,
            cached_only=False,
        )

    match = match_quota_rows(spec, all_rows, group_override=group_override)
    # Only a unique match is worth a request: the ambiguous and empty cases are
    # already an error, and this read is one extra round trip per create.
    workload = workload_for_schedule_type(schedule_config_type)
    if priority_levels_loader is not None:
        levels_by_quota_id = priority_levels_loader()
    else:
        levels_by_quota_id = load_quota_priority_levels(
            workspace_id=workspace_id,
            session=session,
            workload=workload,
        )
    return replace(
        match,
        allowed_priority_levels=allowed_priority_levels_for(
            levels_by_quota_id,
            match.quota_id,
            workload=workload,
        ),
    )


__all__ = [
    "KNOWN_PRIORITY_LEVELS",
    "PRIORITY_LEVELS_ANY_DISPLAY",
    "PRIORITY_LEVELS_UNKNOWN_DISPLAY",
    "PRIORITY_LEVEL_HIGH",
    "PRIORITY_LEVEL_LOW",
    "QuotaCatalogUnavailable",
    "QuotaMatchError",
    "QuotaParseError",
    "QuotaSpec",
    "ResolvedQuota",
    "SCHEDULE_TYPE_DSW",
    "SCHEDULE_TYPE_HPC",
    "SCHEDULE_TYPE_RAY",
    "SCHEDULE_TYPE_TRAIN",
    "SCHEDULE_TYPE_SERVING",
    "allowed_priority_levels_for",
    "build_resource_spec_price",
    "describe_priority_levels",
    "ensure_priority_allowed",
    "load_quota_priority_levels",
    "parse_quota",
    "priority_level_name",
    "resolve_quota",
    "validate_compute_group_name",
    "workload_publishes_priority_levels",
]
