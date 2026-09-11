"""HPC instance identities and selection shared by CLI and SDK."""

from __future__ import annotations
from typing import Any, Sequence
from dataclasses import dataclass
from inspire.platform.web import browser_api as browser_api_module


def hpc_instance_rank(inst: dict[str, Any], position: int) -> int:
    for key in ("rank", "instance_rank", "global_rank", "index", "replica_index"):
        value = inst.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                return int(text)
    return position


def fetch_hpc_instances(
    job_id: str,
    *,
    limit: int,
    session,
    show_all: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch the bounded instance page, expanding it only for explicit ``--all``."""
    rows, total = browser_api_module.list_hpc_job_instances(
        job_id,
        limit=limit,
        session=session,
    )
    if show_all and total > len(rows):
        expanded_rows, expanded_total = browser_api_module.list_hpc_job_instances(
            job_id,
            limit=max(total, len(rows), 1),
            session=session,
        )
        rows = expanded_rows
        total = max(total, expanded_total, len(rows))
    return rows, total


class HPCInstanceSelectionError(ValueError):
    """A ``--instance`` selector matched no instance in the job."""


@dataclass(frozen=True)
class HPCInstanceView:
    """One HPC instance, split into what the Agent sees and what the API needs.

    ``handle`` is the namespaced instance name the platform wants in
    ``ListSlurmdPodEvent`` and ``GetJobLog``. It is a platform handle —
    ``scrub_raw_ids`` reduces it to ``<redacted>-cluster-slurmd-0`` — so it
    never reaches output. ``label`` is the Agent-visible identity and matches
    the Role (plus Rank, when a role has replicas) column of
    ``inspire hpc instances``.
    """

    handle: str
    pod: str
    role: str
    label: str


def hpc_instance_role(inst: dict[str, Any]) -> str:
    for key in ("role", "component", "worker_group_name"):
        value = inst.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def hpc_instance_views(instances: Sequence[dict[str, Any]]) -> list[HPCInstanceView]:
    """Project raw instance rows onto the addressable (label, handle) pairs.

    A role that appears once is its own label. A role with replicas takes the
    Rank suffix, using the same rank ``inspire hpc instances`` prints, so a
    label read off that table addresses the instance it names.
    """
    roles = [hpc_instance_role(inst) for inst in instances]
    duplicated = {role for role in roles if role and roles.count(role) > 1}
    views: list[HPCInstanceView] = []
    for position, (inst, role) in enumerate(zip(instances, roles)):
        handle = str(inst.get("name") or "").strip()
        if not handle:
            continue
        pod = handle.rsplit("/", 1)[-1]
        rank = hpc_instance_rank(inst, position)
        if not role:
            # Without a component the pod has no readable identity of its own;
            # its rank is all `hpc instances` shows, so address it by that.
            role = "instance"
        label = f"{role}-{rank}" if role in duplicated or role == "instance" else role
        views.append(HPCInstanceView(handle=handle, pod=pod, role=role, label=label))
    return views


def select_hpc_instance_views(
    views: Sequence[HPCInstanceView],
    selectors: Sequence[str],
) -> list[HPCInstanceView]:
    """Filter instances by the Role / Rank identity printed by `hpc instances`.

    An unmatched selector raises rather than narrowing the scope to nothing:
    silently returning no pods would make an empty log or event answer look
    like the platform said "there is nothing here".
    """
    if not selectors:
        return list(views)

    available = sorted({view.label for view in views} | {view.role for view in views})
    chosen: list[HPCInstanceView] = []
    for selector in selectors:
        needle = selector.strip().lower()
        matched = [view for view in views if needle in (view.label.lower(), view.role.lower())]
        if not matched:
            raise HPCInstanceSelectionError(
                f"No HPC instance matches '{selector}'. "
                f"Available: {', '.join(available) or '(none)'}."
            )
        chosen.extend(view for view in matched if view not in chosen)
    return chosen
