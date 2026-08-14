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
filters upstream, but this resolver is used by create/profile paths.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from inspire.cli.utils.id_resolver import is_full_uuid, is_stale_handle_error
from inspire.cli.utils.quota_cache import (
    SCHEDULE_TYPE_BY_WORKLOAD,
    CachedPricesLoader,
    group_supports_workload,
    workload_for_schedule_type,
)
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.resource_index import (
    ResourceIdentity,
    ResourceIndex,
    ResourceScope,
    StaleResourceIndexRefresh,
    scope_for_session,
)
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import WebSession, is_transient_api_error

logger = logging.getLogger(__name__)

SCHEDULE_TYPE_DSW = SCHEDULE_TYPE_BY_WORKLOAD["notebook"]
SCHEDULE_TYPE_HPC = SCHEDULE_TYPE_BY_WORKLOAD["hpc"]
SCHEDULE_TYPE_TRAIN = SCHEDULE_TYPE_BY_WORKLOAD["job"]
SCHEDULE_TYPE_SERVING = SCHEDULE_TYPE_BY_WORKLOAD["serving"]
SCHEDULE_TYPE_RAY = SCHEDULE_TYPE_BY_WORKLOAD["ray"]


class QuotaParseError(ValueError):
    """Raised when a ``--quota`` argument cannot be parsed."""


class QuotaMatchError(ValueError):
    """Raised on zero or multi-match of a quota triple inside a workspace."""


class QuotaCatalogUnavailable(ValueError):
    """Raised when the quota catalog could not be read at all.

    Deliberately not a :class:`QuotaMatchError`: no match was ruled out here.
    The platform did not answer, so callers must report an API error rather
    than tell the user their ``--quota`` does not exist.
    """


@dataclass(frozen=True)
class QuotaSpec:
    """A parsed ``--quota`` triple: GPU count, CPU count, memory in GiB."""

    gpu_count: int
    cpu_count: int
    memory_gib: int

    def display(self) -> str:
        return f"{self.gpu_count},{self.cpu_count},{self.memory_gib}"


@dataclass(frozen=True)
class ResolvedQuota:
    """A matched quota row keyed to its platform handles."""

    quota_id: str
    logic_compute_group_id: str
    compute_group_name: str
    gpu_count: int
    cpu_count: int
    memory_gib: int
    gpu_type: str
    raw_price: dict


def parse_quota(text: str) -> QuotaSpec:
    if text is None:
        raise QuotaParseError("--quota is required")
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 3:
        raise QuotaParseError(
            f"--quota expects 'gpu,cpu,mem' (all integers; mem in GiB); got {text!r}"
        )
    try:
        gpu = int(parts[0])
        cpu = int(parts[1])
        mem = int(parts[2])
    except ValueError as exc:
        raise QuotaParseError(
            f"--quota values must be integers; got {text!r}"
        ) from exc
    if gpu < 0 or cpu <= 0 or mem <= 0:
        raise QuotaParseError(
            f"--quota requires gpu>=0, cpu>=1, mem>=1; got gpu={gpu} cpu={cpu} mem={mem}"
        )
    return QuotaSpec(gpu_count=gpu, cpu_count=cpu, memory_gib=mem)


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

QZ_SCHEDULING_ZONE_HINT = (
    "QZ scheduling zones: 开发区 supports both full-node and partial-node GPU "
    "workloads; 训练区 prioritizes full-node workloads, and partial-node GPU "
    "workloads there require LOW priority (1 in fair-scheduling workspaces, preemptible). "
    "Zone semantics "
    "apply per instance/node quota, not aggregate GPU count. Use --group and "
    "--quota from the same live quota row."
)


def qz_scheduling_zone_hint_for_group_names(group_names: Iterable[object]) -> str | None:
    names = [str(name or "") for name in group_names]
    if any(("开发区" in name or "训练区" in name) for name in names):
        return QZ_SCHEDULING_ZONE_HINT
    return None


def validate_compute_group_name(value: str) -> str:
    """Reject platform handles while preserving a user-facing group name."""
    name = str(value or "").strip()
    if not name:
        raise QuotaMatchError("--group value cannot be empty")
    if name.casefold().startswith("lcg-") or is_full_uuid(name):
        raise QuotaMatchError("--group takes a compute group name.")
    return name


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
                    "quota-<workload> --workspace <name> --full`."
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
    cache_index: ResourceIndex | None = None,
) -> ResolvedQuota:
    """Resolve ``spec`` to a unique ``ResolvedQuota`` in ``workspace_id``.

    ``groups`` / ``groups_loader`` / ``prices_loader`` let callers inject
    data (used in tests and to share one prefetched group list between
    multiple calls).
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
            qz_hint = qz_scheduling_zone_hint_for_group_names([group_override, *available])
            raise QuotaMatchError(
                f"No compute group name exactly matches --group {group_override!r}. "
                "Create/profile --group requires the full compute group name. "
                "Use a quota query --group <keyword> only to find the exact name. "
                f"Available: {hint}"
                + (f"\n{qz_hint}" if qz_hint else "")
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

    matches: list[ResolvedQuota] = []
    for group, price in all_rows:
        gpu_count = int(price.get("gpu_count") or 0)
        cpu_count = int(price.get("cpu_count") or 0)
        memory_gib = _extract_memory_gib(price)
        if (gpu_count, cpu_count, memory_gib) != (
            spec.gpu_count,
            spec.cpu_count,
            spec.memory_gib,
        ):
            continue
        quota_id = str(price.get("quota_id") or price.get("spec_id") or "").strip()
        if not quota_id:
            continue
        lcg_id = _group_id(group)
        matches.append(
            ResolvedQuota(
                quota_id=quota_id,
                logic_compute_group_id=lcg_id,
                compute_group_name=_group_name(group),
                gpu_count=gpu_count,
                cpu_count=cpu_count,
                memory_gib=memory_gib,
                gpu_type=_extract_gpu_type(price),
                raw_price=price,
            )
        )

    if not matches:
        qz_hint = qz_scheduling_zone_hint_for_group_names(
            _group_name(group) for group in group_list
        )
        raise QuotaMatchError(
            f"--quota {spec.display()} matches no quota row in the selected workspace."
            f"\nAvailable:\n{_format_row_catalog(all_rows)}"
            + (f"\n{qz_hint}" if qz_hint else "")
        )

    if len(matches) > 1:
        lines = [
            f"  {m.compute_group_name}  (gpu_type={m.gpu_type or 'CPU'})"
            for m in matches
        ]
        qz_hint = qz_scheduling_zone_hint_for_group_names(
            m.compute_group_name for m in matches
        )
        raise QuotaMatchError(
            f"--quota {spec.display()} matches multiple quota rows in the selected workspace; "
            "pass --group <full compute group name> to disambiguate. "
            "Use a quota query --group <keyword> only to find the exact name:\n"
            + "\n".join(lines)
            + (f"\n{qz_hint}" if qz_hint else "")
        )

    return matches[0]


def _format_row_catalog(rows: list[tuple[dict, dict]]) -> str:
    if not rows:
        return "  (workspace has no quotas)"
    lines: list[str] = []
    for group, price in rows:
        gpu_count = int(price.get("gpu_count") or 0)
        cpu_count = int(price.get("cpu_count") or 0)
        memory_gib = _extract_memory_gib(price)
        gpu_type = _extract_gpu_type(price) or "CPU"
        group_name = _group_name(group)
        if not group_name:
            continue
        lines.append(
            f"  {gpu_count},{cpu_count},{memory_gib}  ({gpu_type}, {group_name})"
        )
    return "\n".join(lines)


def build_resource_spec_price(*, quota: ResolvedQuota) -> dict[str, Any]:
    """Build the ``resource_spec_price`` dict the notebook create call expects."""
    price = quota.raw_price if isinstance(quota.raw_price, dict) else {}
    cpu_info_payload = price.get("cpu_info")
    cpu_info: dict[str, Any] = cpu_info_payload if isinstance(cpu_info_payload, dict) else {}
    gpu_info_payload = price.get("gpu_info")
    gpu_info: dict[str, Any] = gpu_info_payload if isinstance(gpu_info_payload, dict) else {}
    machine_gpu_type = str(
        gpu_info.get("gpu_type")
        or price.get("gpu_type")
        or ""
    ).strip()
    if quota.gpu_count > 0 and not machine_gpu_type:
        raise QuotaMatchError(
            "Matched GPU quota is missing machine-readable gpu_info.gpu_type; "
            "cannot build notebook resource_spec_price safely."
        )

    payload = {
        "cpu_type": cpu_info.get("cpu_type", ""),
        "cpu_count": quota.cpu_count,
        "gpu_type": machine_gpu_type,
        "gpu_count": quota.gpu_count,
        "memory_size_gib": quota.memory_gib,
        "logic_compute_group_id": quota.logic_compute_group_id,
        "quota_id": quota.quota_id,
    }
    if quota.gpu_count <= 0:
        payload.pop("gpu_type", None)
    return payload


__all__ = [
    "QuotaCatalogUnavailable",
    "QuotaMatchError",
    "QuotaParseError",
    "QuotaSpec",
    "QZ_SCHEDULING_ZONE_HINT",
    "ResolvedQuota",
    "SCHEDULE_TYPE_DSW",
    "SCHEDULE_TYPE_HPC",
    "SCHEDULE_TYPE_RAY",
    "SCHEDULE_TYPE_TRAIN",
    "build_resource_spec_price",
    "parse_quota",
    "qz_scheduling_zone_hint_for_group_names",
    "resolve_quota",
    "validate_compute_group_name",
]
