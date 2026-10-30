"""Workspace enumeration and scheduling-capability queries via browser API."""

from __future__ import annotations

from typing import Any

from inspire.platform.web.session.models import WebSession
from inspire.platform.web.session.workspaces import (
    ACCESSIBLE_WORKSPACES_PATH,
    WORKSPACE_ID_PATTERN,
    parse_workspace_catalog,
)

from .core import _get_base_url, _request_json


class WorkspaceCapabilityError(RuntimeError):
    """Raised when a write path cannot resolve workspace scheduling policy."""


class WorkspaceEnumerationError(RuntimeError):
    """Raised when the live workspace enumeration request fails."""


def _accessible_workspace_entries(
    session: WebSession,
    *,
    base_url: str | None,
) -> dict[str, dict[str, Any]]:
    resolved_base_url = (base_url or "").strip() or _get_base_url()
    workspace_ids, names, fair_scheduling = parse_workspace_catalog(
        _request_json(
            session,
            "POST",
            ACCESSIBLE_WORKSPACES_PATH,
            referer=f"{resolved_base_url}/jobs/spacesOverview",
            body={},
            timeout=15,
        )
    )
    return {
        workspace_id: {
            "id": workspace_id,
            "name": names[workspace_id],
            "is_fair_workspace": fair_scheduling[workspace_id],
        }
        for workspace_id in workspace_ids
    }


def _cache_fair_scheduling(session: WebSession, results: dict[str, dict[str, Any]]) -> None:
    session.all_workspace_fair_scheduling = {
        ws_id: item["is_fair_workspace"] for ws_id, item in results.items()
    }


def try_enumerate_workspaces(
    session: WebSession,
    base_url: str | None = None,
) -> list[dict[str, Any]]:
    """Try to enumerate workspaces via API endpoints.

    Uses the unscoped ``user.ListAccessibleWorkspaces`` Action. No current
    workspace is required, including immediately after login.

    Returns workspace id, name, and fair-scheduling capability dictionaries.
    Returns an empty list only when the live API successfully reports no
    accessible workspaces. Request and API failures are raised so callers can
    preserve their last successful snapshot.
    """
    try:
        results = _accessible_workspace_entries(
            session,
            base_url=base_url,
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

    if not isinstance(session, WebSession) or not WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
        return False

    try:
        results = _accessible_workspace_entries(
            session,
            base_url=None,
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
__all__ = [
    "WorkspaceCapabilityError",
    "WorkspaceEnumerationError",
    "is_fair_scheduling_workspace",
    "try_enumerate_workspaces",
]
