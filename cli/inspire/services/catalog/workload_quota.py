"""Quota selection helpers shared by workload commands and SDK services."""

from __future__ import annotations
from typing import Any, Mapping, Sequence, Optional
from inspire.services.utils.raw_ids import scrub_raw_ids
from inspire.services.catalog.quotas import QuotaMatchError, ResolvedQuota, QuotaSpec
from inspire.services.catalog.compute_groups import group_supports_workload
from inspire.platform.web.browser_api.availability import QUOTA_PRIORITY_SPEC_FIELDS
from inspire.task_priority import is_low_task_priority

PRIORITY_LEVEL_LOW = "low"
PRIORITY_LEVEL_HIGH = "high"
KNOWN_PRIORITY_LEVELS = frozenset({PRIORITY_LEVEL_LOW, PRIORITY_LEVEL_HIGH})

PRIORITY_LEVELS_UNKNOWN_DISPLAY = "unknown"
PRIORITY_LEVELS_ANY_DISPLAY = "any"


def workload_publishes_priority_levels(workload: str) -> bool:
    """Whether the platform publishes a per-spec priority menu for *workload*."""
    return str(workload or "").strip() in QUOTA_PRIORITY_SPEC_FIELDS


def allowed_priority_levels_for(
    levels_by_quota_id: Mapping[str, tuple[str, ...]] | None,
    quota_id: str,
    *,
    workload: str,
) -> tuple[str, ...] | None:
    """Return one quota row's levels: ``()`` unrestricted, ``None`` unknown."""
    if not workload_publishes_priority_levels(workload):
        # This workload has no spec menu anywhere in the platform's scheduling
        # record, so there is nothing here that could restrict a priority.
        return ()
    if levels_by_quota_id is None:
        return None
    # A spec that is not in the menu is one the workspace said nothing about,
    # and silence is not permission: only a row of the menu is evidence.
    return levels_by_quota_id.get(str(quota_id or "").strip())


def describe_priority_levels(levels: Sequence[str] | None) -> str:
    """Render a quota row's priority menu for a table cell."""
    if levels is None:
        return PRIORITY_LEVELS_UNKNOWN_DISPLAY
    if not levels:
        return PRIORITY_LEVELS_ANY_DISPLAY
    return "/".join(levels)


def priority_level_name(priority: int) -> str:
    """Name the level a numeric task priority falls into."""
    return PRIORITY_LEVEL_LOW if is_low_task_priority(priority) else PRIORITY_LEVEL_HIGH


def ensure_priority_allowed(
    quota: ResolvedQuota,
    priority: object,
    *,
    quota_command: str = "the workload's `quota` command",
) -> None:
    """Refuse a create the workspace's own spec menu says it will reject.

    Silence never blocks: an unknown menu (``None``) and an unrestricted one
    (``()``) both pass, because turning one unanswered request into a refused
    create would make a platform hiccup indistinguishable from a quota the user
    may not have.
    """
    levels = quota.allowed_priority_levels
    if not levels:
        return
    if isinstance(priority, bool) or not isinstance(priority, int):
        return
    if any(level not in KNOWN_PRIORITY_LEVELS for level in levels):
        # An unrecognised vocabulary is not something to enforce blindly.
        return
    if priority_level_name(priority) in levels:
        return
    allowed_text = "/".join(level.upper() for level in levels)
    raise QuotaMatchError(
        f"Quota {quota.gpu_count},{quota.cpu_count},{quota.memory_gib} in compute group "
        f"{quota.compute_group_name!r} is published as {allowed_text}-priority only, and "
        f"--priority {priority} is {priority_level_name(priority).upper()}. The platform "
        "would reject this create. Either pass --priority 1 (LOW, preemptible in "
        "fair-scheduling workspaces), or pick a quota row whose Priority column reads "
        f"'{PRIORITY_LEVELS_ANY_DISPLAY}' -- the restriction is per quota row, so a larger "
        f"row in the same compute group is often unrestricted. See `{quota_command}`."
    )


def selected_groups(groups, workload: str, keyword: str = ""):
    for group in groups:
        if group_supports_workload(group, workload) and (
            not keyword
            or keyword.casefold()
            in str(group.get("name") or group.get("logic_compute_group_name") or "").casefold()
        ):
            yield group


def quota_values(price: dict[str, Any]) -> tuple[int, int, int, str]:
    info = price.get("gpu_info") or {}
    return (
        int(price.get("gpu_count") or 0),
        int(price.get("cpu_count") or 0),
        int(
            price.get("memory_size_gib")
            or price.get("memory_size")
            or price.get("memory_size_gb")
            or 0
        ),
        str(
            info.get("gpu_type_display")
            or info.get("gpu_type")
            or info.get("brand_name")
            or price.get("gpu_type")
            or ("CPU" if not price.get("gpu_count") else "")
        ),
    )


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
        price.get("memory_size_gib") or price.get("memory_size") or price.get("memory_size_gb") or 0
    )
    try:
        return int(value)
    except Exception:
        return 0


def _group_id(group: dict) -> str:
    return str(group.get("logic_compute_group_id") or group.get("id") or "").strip()


def _group_name(group: dict, fallback: str = "") -> str:
    return str(group.get("name") or group.get("logic_compute_group_name") or fallback).strip()


def match_quota_rows(
    spec: QuotaSpec, all_rows: list[tuple[dict, dict]], *, group_override: str | None = None
) -> ResolvedQuota:
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
        raise QuotaMatchError(
            f"--quota {spec.display()} matches no quota row in the selected workspace."
            f"\nAvailable:\n{format_row_catalog(all_rows, group_override=group_override)}"
        )

    if len(matches) > 1:
        lines = [f"  {m.compute_group_name}  (gpu_type={m.gpu_type or 'CPU'})" for m in matches]
        raise QuotaMatchError(
            f"--quota {spec.display()} matches multiple quota rows in the selected workspace; "
            "pass --group <full compute group name> to disambiguate. "
            "Use a quota query --group <keyword> only to find the exact name:\n" + "\n".join(lines)
        )

    return matches[0]


def format_row_catalog(
    rows: list[tuple[dict, dict]],
    *,
    group_override: Optional[str] = None,
) -> str:
    if not rows:
        # The row set was already narrowed to `--group` by the time we get
        # here, so "the workspace has no quotas" would blame the wrong scope
        # and send the reader looking for a problem that is not there.
        if group_override:
            return f"  (compute group {group_override!r} has no quota rows for this workload)"
        return "  (no quota rows in this workspace for this workload)"
    lines: list[str] = []
    for group, price in rows:
        gpu_count = int(price.get("gpu_count") or 0)
        cpu_count = int(price.get("cpu_count") or 0)
        memory_gib = _extract_memory_gib(price)
        gpu_type = _extract_gpu_type(price) or "CPU"
        group_name = _group_name(group)
        if not group_name:
            continue
        lines.append(f"  {gpu_count},{cpu_count},{memory_gib}  ({gpu_type}, {group_name})")
    return "\n".join(lines)


def _extract_points_per_hour(price: dict[str, Any]) -> float | None:
    """Read the row's 点券 (compute credit) cost per instance-hour.

    ``0`` and "the platform did not price this row" are different answers:
    every CPU-only row really is free, so collapsing a missing field into
    zero would advertise a GPU row as free the one time the field is absent.
    """
    value = price.get("total_price_per_hour")
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def query_workspace_quotas(
    *,
    workspace_name: str,
    workload: str,
    group_filter: str,
    include_empty: bool,
    groups: list[dict[str, Any]],
    load_prices,
    load_levels,
    include_identity: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_rows: set[tuple[str, int, int, int, str, str]] = set()
    public_workspace_name = scrub_raw_ids(workspace_name)
    # One request for the whole workspace, and only once a row needs it: a
    # `--workspace all` sweep with a `--group` filter would otherwise pay for
    # every workspace whose groups it then skips.
    menu: list[dict[str, tuple[str, ...]] | None] = []

    def priority_levels() -> dict[str, tuple[str, ...]] | None:
        if not menu:
            menu.append(load_levels())
        return menu[0]

    for item in selected_groups(groups, workload, group_filter):
        logic_compute_group_id = _group_id(item)
        if not logic_compute_group_id:
            continue
        compute_group_name = _group_name(item, fallback="")
        if not compute_group_name:
            continue

        prices = load_prices(logic_compute_group_id)
        if not prices:
            if include_empty:
                rows.append(
                    {
                        **(
                            {"quota_id": "", "group_id": logic_compute_group_id}
                            if include_identity
                            else {}
                        ),
                        "workspace": public_workspace_name,
                        "compute_group": scrub_raw_ids(compute_group_name),
                        "gpu_type": "",
                        "quota": "",
                        "priority": "",
                        "allowed_priority_levels": None,
                        "points_per_hour": None,
                    }
                )
            continue

        for price in prices:
            gpu_count, cpu_count, memory_size_gib, gpu_type = quota_values(price)
            quota_id = str(price.get("quota_id") or price.get("spec_id") or "").strip()
            levels = allowed_priority_levels_for(priority_levels(), quota_id, workload=workload)
            priority = describe_priority_levels(levels)
            # Two rows that differ only in what priorities they accept are two
            # different offers, so the restriction is part of the identity.
            key = (
                compute_group_name,
                gpu_count,
                cpu_count,
                memory_size_gib,
                gpu_type,
                priority,
            )
            if key in seen_rows:
                continue
            seen_rows.add(key)
            rows.append(
                {
                    **(
                        {"quota_id": quota_id, "group_id": logic_compute_group_id}
                        if include_identity
                        else {}
                    ),
                    "workspace": public_workspace_name,
                    "compute_group": scrub_raw_ids(compute_group_name),
                    "gpu_type": scrub_raw_ids(gpu_type),
                    "quota": f"{gpu_count},{cpu_count},{memory_size_gib}",
                    "priority": priority,
                    # `null` is "the platform did not answer", `[]` is "no
                    # restriction"; a consumer that collapses them is wrong.
                    "allowed_priority_levels": list(levels) if levels is not None else None,
                    "points_per_hour": _extract_points_per_hour(price),
                }
            )
    return rows


def sort_quota_rows(rows: list[dict[str, Any]]) -> None:
    rows.sort(
        key=lambda r: (
            str(r.get("workspace", "")),
            str(r.get("compute_group", "")),
            str(r.get("gpu_type", "")),
            str(r.get("quota", "")),
        )
    )
