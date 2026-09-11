"""Ray instance identities and selection shared by CLI and SDK."""

from __future__ import annotations
from typing import Any, Sequence
from dataclasses import dataclass
from inspire.services.utils.raw_ids import scrub_raw_ids
from inspire.platform.web import browser_api as browser_api_module


def public_ray_instance_text(inst: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = inst.get(key)
        if value in (None, "") or isinstance(value, (dict, list, tuple, set)):
            continue
        text = scrub_raw_ids(value).strip()
        if text and "<redacted>" not in text:
            return text
    return ""


def ray_instance_rank(inst: dict[str, Any], position: int) -> int:
    for key in ("rank", "instance_rank", "global_rank", "index", "replica_index"):
        value = inst.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                return int(text)
    return position


class RayInstanceSelectionError(ValueError):
    """A ``--instance`` selector matched no instance in the Ray cluster."""


@dataclass(frozen=True)
class RayInstanceView:
    """One Ray pod, split into what the Agent sees and what the API needs.

    ``handle`` is the pod name ``GetJobLog`` scopes on. It is a platform
    handle — ``scrub_raw_ids`` reduces it to noise — so it never reaches
    output. ``label`` is the Agent-visible identity and matches the Role /
    Type (plus Rank, when several pods share one) columns of
    ``inspire ray instances``.
    """

    handle: str
    role: str
    kind: str
    label: str


def ray_instance_kind(inst: dict[str, Any]) -> str:
    """Read head / worker off the row, matching the Type column."""
    return public_ray_instance_text(inst, "type", "instance_type")


def ray_instance_role(inst: dict[str, Any]) -> str:
    """Read the worker-group identity off the row, matching the Role column."""
    return public_ray_instance_text(inst, "role", "worker_group_name", "component")


def ray_instance_views(
    instances: Sequence[dict[str, Any]],
) -> list[RayInstanceView]:
    """Project raw pod rows onto the addressable (label, handle) pairs.

    Ray's readable identity is two-level: every pod is a ``head`` or a
    ``worker``, and every worker also belongs to a named worker group. Both
    are worth selecting on — "what did the head driver print" and "what did
    the decode group print" are the two questions this view exists to answer —
    so an identity that appears once becomes its own label and one with
    replicas takes the Rank suffix ``inspire ray instances`` already prints.
    """
    identities = [
        ray_instance_role(inst) or ray_instance_kind(inst) or "instance" for inst in instances
    ]
    duplicated = {name for name in identities if identities.count(name) > 1}
    views: list[RayInstanceView] = []
    for position, (inst, identity) in enumerate(zip(instances, identities)):
        # Raw on purpose: this is the pod name `GetJobLog` scopes on, so it
        # must not go through `scrub_raw_ids` the way the printed fields do.
        handle = next(
            (
                str(inst.get(key) or "").strip()
                for key in ("name", "instance_name", "pod_name")
                if str(inst.get(key) or "").strip()
            ),
            "",
        )
        if not handle:
            continue
        rank = ray_instance_rank(inst, position)
        label = f"{identity}-{rank}" if identity in duplicated else identity
        views.append(
            RayInstanceView(
                handle=handle,
                role=ray_instance_role(inst),
                kind=ray_instance_kind(inst),
                label=label,
            )
        )
    return views


def select_ray_instance_views(
    views: Sequence[RayInstanceView],
    selectors: Sequence[str],
) -> list[RayInstanceView]:
    """Filter pods by the Role / Type / Rank identity ``ray instances`` prints.

    An unmatched selector raises rather than narrowing the scope to nothing:
    ``ray.GetJobLog`` answers an empty pod list with a clean empty result, so
    silently dropping every pod would read as "this cluster printed nothing".
    """
    if not selectors:
        return list(views)

    available = sorted(
        {view.label for view in views}
        | {view.role for view in views if view.role}
        | {view.kind for view in views if view.kind}
    )
    chosen: list[RayInstanceView] = []
    for selector in selectors:
        needle = selector.strip().lower()
        matched = [
            view
            for view in views
            if needle in (view.label.lower(), view.role.lower(), view.kind.lower())
        ]
        if not matched:
            raise RayInstanceSelectionError(
                f"No Ray instance matches '{selector}'. "
                f"Available: {', '.join(available) or '(none)'}."
            )
        chosen.extend(view for view in matched if view not in chosen)
    return chosen


def fetch_ray_instances(
    ray_job_id: str,
    *,
    limit: int,
    session,
    show_all: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch the bounded instance page, expanding it only for explicit ``--all``."""
    rows, total = browser_api_module.list_ray_job_instances(
        ray_job_id,
        limit=limit,
        session=session,
    )
    if show_all and total > len(rows):
        expanded_rows, expanded_total = browser_api_module.list_ray_job_instances(
            ray_job_id,
            limit=max(total, len(rows), 1),
            session=session,
        )
        rows = expanded_rows
        total = max(total, expanded_total, len(rows))
    return rows, total
