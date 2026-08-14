"""Workspace enumeration and quota queries via browser API endpoints."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from inspire.platform.web.session.models import DEFAULT_WORKSPACE_ID, WebSession

from .core import _get_base_url, _request_json, _v2_result

_WS_ID_RE = re.compile(r"^ws-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class WorkspaceCapabilityError(RuntimeError):
    """Raised when a write path cannot resolve workspace scheduling policy."""


class WorkspaceEnumerationError(RuntimeError):
    """Raised when the live workspace enumeration request fails."""


def _workspace_route_entries(
    session: WebSession,
    *,
    base_url: str | None,
    workspace_id: str | None,
) -> dict[str, dict[str, Any]]:
    resolved_base_url = (base_url or "").strip() or _get_base_url()
    referer = f"{resolved_base_url}/jobs/distributedTraining"

    probe_ws_id = (workspace_id or "").strip()
    if not probe_ws_id or not _WS_ID_RE.match(probe_ws_id):
        probe_ws_id = str(getattr(session, "workspace_id", None) or "").strip()
    if not probe_ws_id or probe_ws_id == DEFAULT_WORKSPACE_ID:
        raise WorkspaceCapabilityError("No workspace is available for capability lookup.")

    payload = _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/user?Action=GetRoutes",
            referer=referer,
            body={"WorkspaceId": probe_ws_id},
            timeout=15,
        )
    )
    results: dict[str, dict[str, Any]] = {}
    for route_group in payload.get("routes") or []:
        if not isinstance(route_group, dict) or route_group.get("name") != "userWorkspaceList":
            continue
        for entry in route_group.get("routes") or []:
            if not isinstance(entry, dict):
                continue
            ws_id = str(entry.get("path") or "").strip()
            ws_name = str(entry.get("name") or "").strip()
            if ws_id and _WS_ID_RE.match(ws_id) and ws_id != DEFAULT_WORKSPACE_ID:
                results[ws_id] = {
                    "id": ws_id,
                    "name": ws_name,
                    "is_fair_workspace": entry.get("is_fair_workspace") is True,
                }
    return results


def _cache_fair_scheduling(session: WebSession, results: dict[str, dict[str, Any]]) -> None:
    if not results:
        return
    cached = dict(getattr(session, "all_workspace_fair_scheduling", None) or {})
    cached.update({ws_id: bool(item["is_fair_workspace"]) for ws_id, item in results.items()})
    session.all_workspace_fair_scheduling = cached


def try_enumerate_workspaces(
    session: WebSession,
    base_url: str | None = None,
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    """Try to enumerate workspaces via API endpoints.

    Primary method: ``user.GetRoutes``, which returns a ``userWorkspaceList``
    route group containing all workspaces the user can access. The Action is
    absent from discovery but live, and answers the v1
    ``/user/routes/{workspace_id}`` payload verbatim — ``is_fair_workspace``
    included.

    Returns workspace id, name, and fair-scheduling capability dictionaries.
    Returns an empty list only when the live API successfully reports no
    accessible workspaces. Request and API failures are raised so callers can
    preserve their last successful snapshot.
    """
    try:
        results = _workspace_route_entries(
            session,
            base_url=base_url,
            workspace_id=workspace_id,
        )
    except Exception as exc:
        raise WorkspaceEnumerationError(
            "Could not enumerate accessible workspaces."
        ) from exc

    _cache_fair_scheduling(session, results)
    return list(results.values())


def is_fair_scheduling_workspace(session: WebSession, workspace_id: str) -> bool:
    """Return the live workspace capability used by qz priority selectors."""
    cached = getattr(session, "all_workspace_fair_scheduling", None) or {}
    if workspace_id in cached:
        return cached[workspace_id] is True

    if not isinstance(session, WebSession) or not _WS_ID_RE.match(workspace_id):
        return False

    try:
        results = _workspace_route_entries(
            session,
            base_url=None,
            workspace_id=workspace_id,
        )
    except Exception as exc:
        raise WorkspaceCapabilityError(
            "Could not resolve the selected workspace's scheduling policy."
        ) from exc
    _cache_fair_scheduling(session, results)
    if workspace_id not in results:
        raise WorkspaceCapabilityError(
            "Could not resolve the selected workspace's scheduling policy."
        )
    return bool(results[workspace_id]["is_fair_workspace"])


# `-1` is how the platform spells "no ceiling" in every quota field.
UNLIMITED_QUOTA = -1


@dataclass(frozen=True)
class WorkspaceQuotaUsage:
    """One resource's quota ceiling and current draw in a workspace.

    ``limit`` is ``UNLIMITED_QUOTA`` (-1) when the platform sets no ceiling,
    and ``capacity`` is what the workspace physically has. The two answer
    different questions: a submit can be refused because the quota is spent
    even while nodes sit idle, and it can be refused because the nodes are
    busy even while quota remains.
    """

    resource: str
    limit: float
    used: float
    capacity: Optional[float] = None
    capacity_used: Optional[float] = None

    @property
    def unlimited(self) -> bool:
        return self.limit == UNLIMITED_QUOTA

    @property
    def available(self) -> Optional[float]:
        if self.unlimited:
            return None
        return self.limit - self.used


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _optional_number(payload: dict[str, Any], key: str) -> Optional[float]:
    value = payload.get(key)
    if value in (None, ""):
        return None
    return _number(value)


def get_workspace_quota_usage(
    workspace_id: str,
    *,
    session: WebSession,
    priority: str = "high",
) -> list[WorkspaceQuotaUsage]:
    """Read a workspace's quota ceilings, current draw, and physical capacity.

    Two Actions, one call each, both available to an ordinary workspace member:
    ``workspace.GetWorkspaceQuota`` for the quota ceiling plus what is already
    drawn against it, and ``workspace.GetWorkspaceComputeResource`` for the
    physical totals. Both take a **top-level** ``workspace_id``; the nested
    ``filter`` envelope other ``workspace.*`` Actions want is rejected here.

    ``priority`` selects which half of the quota to report. The platform keeps
    a separate ceiling for high-priority (guaranteed) and low-priority
    (reclaimable) work, and a running task draws against exactly one of them,
    so mixing the two would misreport both.

    The task-level relatives of these Actions -- ``GetWorkspaceTaskQuota``,
    ``GetUserTaskQuota``, ``GetDefaultUserTaskQuota`` and ``ListUserQuotas`` --
    all answer ``AccessForbidden`` to a non-admin and are deliberately not
    wrapped.
    """
    prefix = "high" if str(priority).lower() != "low" else "low"
    referer = f"{_get_base_url()}/jobs/distributedTraining"

    quota = _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/workspace?Action=GetWorkspaceQuota",
            referer=referer,
            body={"workspace_id": workspace_id},
            timeout=20,
        )
    )
    compute = _v2_result(
        _request_json(
            session,
            "POST",
            "/api/v2/workspace?Action=GetWorkspaceComputeResource",
            referer=referer,
            body={"workspace_id": workspace_id},
            timeout=20,
        )
    )
    # The platform spells the key `logic_resouces`; that typo is the wire
    # format, not ours.
    resources = compute.get("logic_resouces")
    if not isinstance(resources, dict):
        resources = {}

    rows: list[WorkspaceQuotaUsage] = []
    for resource, quota_key, total_key, used_key in (
        ("gpu", "gpu", "gpu_total", "gpu_used"),
        ("cpu", "cpu", "cpu_total", "cpu_used"),
        ("memory_gib", "memory", "memory_gi_total", "memory_gi_used"),
    ):
        rows.append(
            WorkspaceQuotaUsage(
                resource=resource,
                limit=_number(quota.get(f"{quota_key}_{prefix}_running")),
                used=_number(quota.get(f"{quota_key}_{prefix}_running_used")),
                capacity=_optional_number(resources, total_key),
                capacity_used=_optional_number(resources, used_key),
            )
        )
    return rows


__all__ = [
    "UNLIMITED_QUOTA",
    "WorkspaceCapabilityError",
    "WorkspaceEnumerationError",
    "WorkspaceQuotaUsage",
    "get_workspace_quota_usage",
    "is_fair_scheduling_workspace",
    "try_enumerate_workspaces",
]
