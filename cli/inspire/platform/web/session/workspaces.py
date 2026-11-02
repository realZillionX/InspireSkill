"""Workspace discovery contract shared by login and Browser API queries."""

from __future__ import annotations

import re
from typing import Any

from .envelope import _v2_result
from .models import DEFAULT_WORKSPACE_ID

ACCESSIBLE_WORKSPACES_PATH = "/api/v2/user?Action=ListAccessibleWorkspaces"
WORKSPACE_ID_PATTERN = re.compile(
    r"^ws-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def parse_workspace_catalog(
    payload: dict[str, Any],
) -> tuple[list[str], dict[str, str], dict[str, bool]]:
    """Parse a complete catalog; malformed responses must not become empty scans."""
    items = _v2_result(payload).get("items")
    if not isinstance(items, list):
        raise ValueError("Workspace discovery response is missing an items list.")

    names: dict[str, str] = {}
    fair_scheduling: dict[str, bool] = {}
    for entry in items:
        if not isinstance(entry, dict):
            raise ValueError("Workspace discovery returned an invalid entry.")
        workspace_id = entry.get("workspace_id")
        name = entry.get("name")
        fair = entry.get("is_fair_workspace", False)
        if (
            not isinstance(workspace_id, str)
            or not WORKSPACE_ID_PATTERN.fullmatch(workspace_id)
            or workspace_id == DEFAULT_WORKSPACE_ID
            or not isinstance(name, str)
            or not name.strip()
            or not isinstance(fair, bool)
        ):
            raise ValueError("Workspace discovery returned an invalid entry.")
        names[workspace_id] = name.strip()
        fair_scheduling[workspace_id] = fair

    return list(names), names, fair_scheduling
