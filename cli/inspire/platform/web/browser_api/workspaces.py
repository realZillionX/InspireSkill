"""Workspace enumeration via browser API endpoints."""

from __future__ import annotations

import re
from typing import Any

from inspire.platform.web.session.models import DEFAULT_WORKSPACE_ID, WebSession

from .core import _browser_api_path, _get_base_url, _request_json

_WS_ID_RE = re.compile(r"^ws-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class WorkspaceCapabilityError(RuntimeError):
    """Raised when a write path cannot resolve workspace scheduling policy."""


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

    resp = _request_json(
        session,
        "GET",
        _browser_api_path(f"/user/routes/{probe_ws_id}"),
        referer=referer,
        timeout=15,
    )
    results: dict[str, dict[str, Any]] = {}
    for route_group in (resp.get("data") or {}).get("routes") or []:
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

    Primary method: ``GET /api/v1/user/routes/{workspace_id}`` which returns
    a ``userWorkspaceList`` route group containing all workspaces the user
    can access.

    Returns workspace id, name, and fair-scheduling capability dictionaries.
    Gracefully returns an empty list on any failure.
    """
    try:
        results = _workspace_route_entries(
            session,
            base_url=base_url,
            workspace_id=workspace_id,
        )
    except Exception:
        return []

    _cache_fair_scheduling(session, results)
    return list(results.values())


def is_fair_scheduling_workspace(session: WebSession, workspace_id: str) -> bool:
    """Return the live workspace capability used by qz priority selectors."""
    cached = getattr(session, "all_workspace_fair_scheduling", None) or {}
    if workspace_id in cached:
        return bool(cached[workspace_id])

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


__all__ = [
    "WorkspaceCapabilityError",
    "is_fair_scheduling_workspace",
    "try_enumerate_workspaces",
]
