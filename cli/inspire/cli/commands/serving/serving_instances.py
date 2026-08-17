"""Addressable identities for inference-serving instances.

A serving pod is named ``<project>/sv-<uuid>-0``. That is a platform handle:
it never reaches output, so `inspire serving instances` identifies a row by
its Rank. Selectors have to speak that same identity, and the platform Actions
have to receive the namespaced handle — a bare pod name answers
``InternalError`` on both the log and the event endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


class ServingInstanceSelectionError(ValueError):
    """A `--instance` selector matched no instance in the deployment."""


@dataclass
class ServingInstanceView:
    """One serving pod: the Agent-visible label and the API-visible handle."""

    handle: str
    label: str
    role: str


def serving_instance_views(
    instances: Sequence[dict[str, Any]],
) -> list[ServingInstanceView]:
    """Project instance rows onto the (label, handle) pairs commands address."""
    # Imported here, not at module scope: the projection lives in the command
    # module that imports this one.
    from inspire.cli.commands.serving.serving_commands import _public_serving_instances

    views: list[ServingInstanceView] = []
    rows = list(instances)
    for public, raw in zip(_public_serving_instances(rows), rows):
        handle = str(raw.get("name") or raw.get("pod_name") or "").strip()
        if not handle:
            continue
        label = str(public.get("name") or "").strip()
        if not label:
            rank = public.get("rank")
            if rank is None:
                continue
            label = f"rank={rank}"
        views.append(
            ServingInstanceView(
                handle=handle,
                label=label,
                role=str(public.get("role") or "").strip(),
            )
        )
    return views


def select_serving_instance_views(
    views: list[ServingInstanceView],
    selectors: Sequence[str],
) -> list[ServingInstanceView]:
    """Resolve `--instance` selectors, or fail instead of emptying the scope.

    ``rank=0`` and a bare ``0`` address the same replica; a role name
    (``LEADER``) selects every pod in that role. An unmatched selector raises,
    because both endpoints answer an unknown pod list with a clean empty
    result that reads as "this replica said nothing".
    """
    if not selectors:
        return list(views)

    selected: list[ServingInstanceView] = []
    seen: set[str] = set()
    for selector in selectors:
        needle = str(selector or "").strip().lower()
        if not needle:
            continue
        bare_rank = f"rank={needle}" if needle.isdigit() else ""
        matches = [
            view
            for view in views
            if view.label.lower() == needle
            or (bare_rank and view.label.lower() == bare_rank)
            or (view.role and view.role.lower() == needle)
        ]
        if not matches:
            known = ", ".join(view.label for view in views) or "none"
            raise ServingInstanceSelectionError(
                f"No serving instance matches {selector!r}. Known instances: {known}. "
                "List them with `inspire serving instances <serving-name> "
                "--workspace <workspace>`."
            )
        for view in matches:
            if view.handle not in seen:
                seen.add(view.handle)
                selected.append(view)
    return selected
