"""Workspace enumeration via browser API endpoints."""

from __future__ import annotations

import re
from typing import Any

from inspire.platform.web.session.models import DEFAULT_WORKSPACE_ID, WebSession

from .core import _browser_api_path, _get_base_url, _request_json

_WS_ID_RE = re.compile(r"^ws-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def try_enumerate_workspaces(
    session: WebSession,
    base_url: str | None = None,
    workspace_id: str | None = None,
) -> list[dict[str, Any]]:
    """Try to enumerate workspaces via API endpoints.

    Primary method: ``GET /api/v1/user/routes/{workspace_id}`` which returns
    a ``userWorkspaceList`` route group containing all workspaces the user
    can access.

    Returns a list of ``{"id": "ws-...", "name": "..."}`` dicts.
    Gracefully returns an empty list on any failure.
    """
    resolved_base_url = (base_url or "").strip() or _get_base_url()
    referer = f"{resolved_base_url}/jobs/distributedTraining"
    results: dict[str, dict[str, Any]] = {}

    # Resolve a workspace_id to use as the routes endpoint parameter
    probe_ws_id = (workspace_id or "").strip()
    if not probe_ws_id or not _WS_ID_RE.match(probe_ws_id):
        probe_ws_id = str(session.workspace_id or "").strip()
    if not probe_ws_id or probe_ws_id == DEFAULT_WORKSPACE_ID:
        return []

    # Primary: GET /api/v1/user/routes/{workspace_id}
    try:
        resp = _request_json(
            session,
            "GET",
            _browser_api_path(f"/user/routes/{probe_ws_id}"),
            referer=referer,
            timeout=15,
        )
        for route_group in (resp.get("data") or {}).get("routes") or []:
            if not isinstance(route_group, dict):
                continue
            if route_group.get("name") != "userWorkspaceList":
                continue
            for entry in route_group.get("routes") or []:
                if not isinstance(entry, dict):
                    continue
                ws_id = str(entry.get("path") or "").strip()
                ws_name = str(entry.get("name") or "").strip()
                if ws_id and _WS_ID_RE.match(ws_id) and ws_id != DEFAULT_WORKSPACE_ID:
                    results[ws_id] = {"id": ws_id, "name": ws_name}
    except Exception:
        pass

    return list(results.values())
