"""Resolve a ``(gpu, cpu, memory_gib)`` triple to a unique platform ``quota_id``.

Quotas on Inspire are flat rows exposed by
``POST /resource_prices/logic_compute_groups/``. Each row has a
``quota_id`` plus ``(gpu_count, cpu_count, memory_size_gib, gpu_info)``.
The user passes the triple; this module queries every compute group in
the workspace, filters for rows whose three numbers match exactly, and
demands exactly one row survives. GPU type falls out of the matched row.

When multiple compute groups in the same workspace expose the same
triple (e.g. an H100 group and an H200 group both offering
``(1, 20, 200)``), the caller must pass ``--group`` to disambiguate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import WebSession

SCHEDULE_TYPE_DSW = "SCHEDULE_CONFIG_TYPE_DSW"
SCHEDULE_TYPE_HPC = "SCHEDULE_CONFIG_TYPE_HPC"
SCHEDULE_TYPE_TRAIN = "SCHEDULE_CONFIG_TYPE_TRAIN"
SCHEDULE_TYPE_RAY = "SCHEDULE_CONFIG_TYPE_RAY"


class QuotaParseError(ValueError):
    """Raised when a ``--quota`` argument cannot be parsed."""


class QuotaMatchError(ValueError):
    """Raised on zero or multi-match of a quota triple inside a workspace."""


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
    """A matched quota row keyed to its platform IDs."""

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


def _extract_gpu_type(price: dict) -> str:
    gpu_info = price.get("gpu_info") if isinstance(price.get("gpu_info"), dict) else {}
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


def _default_groups_loader(
    *, workspace_id: str, session: WebSession
) -> GroupsLoader:
    def loader() -> list[dict]:
        return browser_api_module.list_notebook_compute_groups(
            workspace_id=workspace_id,
            session=session,
        )

    return loader


def _default_prices_loader(
    *, workspace_id: str, session: WebSession, schedule_config_type: str
) -> PricesLoader:
    def loader(lcg_id: str) -> list[dict]:
        return browser_api_module.get_resource_prices(
            workspace_id=workspace_id,
            logic_compute_group_id=lcg_id,
            schedule_config_type=schedule_config_type,
            session=session,
        )

    return loader


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
) -> ResolvedQuota:
    """Resolve ``spec`` to a unique ``ResolvedQuota`` in ``workspace_id``.

    ``groups`` / ``groups_loader`` / ``prices_loader`` let callers inject
    data (used in tests and to share one prefetched group list between
    multiple calls).
    """
    if groups is not None:
        group_list = list(groups)
    else:
        loader = groups_loader
        if loader is None:
            if session is None:
                raise ValueError("resolve_quota needs a session or groups/groups_loader")
            loader = _default_groups_loader(
                workspace_id=workspace_id, session=session
            )
        group_list = list(loader())

    if group_override:
        target = group_override.strip()
        if not target:
            raise QuotaMatchError("--group value cannot be empty")
        target_lower = target.lower()
        filtered = []
        for group in group_list:
            name = _group_name(group)
            if name.lower() == target_lower:
                filtered = [group]
                break
            if target_lower in name.lower():
                filtered.append(group)
        if not filtered:
            available = sorted({
                _group_name(g) for g in group_list if _group_name(g)
            })
            hint = ", ".join(available) if available else "(none)"
            raise QuotaMatchError(
                f"No compute group name matches --group {group_override!r}. "
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
        )

    all_rows: list[tuple[dict, dict]] = []
    for group in group_list:
        lcg_id = _group_id(group)
        if not lcg_id:
            continue
        try:
            prices = prices_loader(lcg_id)
        except Exception:
            prices = []
        for price in prices or []:
            all_rows.append((group, price))

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
                compute_group_name=_group_name(group, fallback=lcg_id),
                gpu_count=gpu_count,
                cpu_count=cpu_count,
                memory_gib=memory_gib,
                gpu_type=_extract_gpu_type(price),
                raw_price=price,
            )
        )

    if not matches:
        raise QuotaMatchError(
            f"--quota {spec.display()} matches no spec in workspace {workspace_id}."
            f"\nAvailable:\n{_format_row_catalog(all_rows)}"
        )

    if len(matches) > 1:
        lines = [
            f"  {m.compute_group_name}  (gpu_type={m.gpu_type or 'CPU'}, quota_id={m.quota_id})"
            for m in matches
        ]
        raise QuotaMatchError(
            f"--quota {spec.display()} matches multiple specs in workspace "
            f"{workspace_id}; pass --group to disambiguate:\n" + "\n".join(lines)
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
        group_name = _group_name(group, fallback=_group_id(group))
        lines.append(
            f"  {gpu_count},{cpu_count},{memory_gib}  ({gpu_type}, {group_name})"
        )
    return "\n".join(lines)


def build_resource_spec_price(
    *, quota: ResolvedQuota, shared_memory_size: Optional[int] = None
) -> dict[str, Any]:
    """Build the ``resource_spec_price`` dict the notebook create endpoint expects."""
    del shared_memory_size  # kept for symmetry; backend reads shared_memory_size elsewhere
    cpu_info = (
        quota.raw_price.get("cpu_info")
        if isinstance(quota.raw_price.get("cpu_info"), dict)
        else {}
    )
    return {
        "cpu_type": cpu_info.get("cpu_type", ""),
        "cpu_count": quota.cpu_count,
        "gpu_type": quota.gpu_type,
        "gpu_count": quota.gpu_count,
        "memory_size_gib": quota.memory_gib,
        "logic_compute_group_id": quota.logic_compute_group_id,
        "quota_id": quota.quota_id,
    }


__all__ = [
    "QuotaMatchError",
    "QuotaParseError",
    "QuotaSpec",
    "ResolvedQuota",
    "SCHEDULE_TYPE_DSW",
    "SCHEDULE_TYPE_HPC",
    "SCHEDULE_TYPE_RAY",
    "SCHEDULE_TYPE_TRAIN",
    "build_resource_spec_price",
    "parse_quota",
    "resolve_quota",
]
