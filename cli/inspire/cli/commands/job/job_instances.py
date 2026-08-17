"""Addressable identities for training-job instances.

A training pod is named `job-<uuid>-worker-0-0`. That is a platform handle:
`scrub_raw_ids` reduces it to `<redacted>-worker-0-0`, so `inspire job
instances` drops the name entirely and identifies a row by its Rank. Selectors
therefore cannot take pod names — the CLI never prints one, which left
`--instance` with no discoverable value at all.

This module is the translation the HPC and Ray commands already have: the
Agent-visible label on one side, the pod handle the platform Actions want on
the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from inspire.cli.commands.job.job_commands import _public_job_instances


class JobInstanceSelectionError(ValueError):
    """A `--instance` selector matched no instance in the job."""


@dataclass
class JobInstanceView:
    """One instance, split into what the Agent sees and what the API needs."""

    handle: str
    label: str
    role: str


def job_instance_views(instances: Sequence[dict[str, Any]]) -> list[JobInstanceView]:
    """Project instance rows onto the (label, handle) pairs commands address.

    The label is exactly what `inspire job instances` prints in its Name
    column: the platform's own name when it survives the output boundary, and
    ``rank=N`` when it does not.
    """
    views: list[JobInstanceView] = []
    rows = list(instances)
    for public, raw in zip(_public_job_instances(rows), rows):
        handle = str(raw.get("name") or "").strip()
        if not handle:
            continue
        label = str(public.get("name") or "").strip()
        if not label:
            rank = public.get("rank")
            if rank is None:
                continue
            label = f"rank={rank}"
        views.append(
            JobInstanceView(
                handle=handle,
                label=label,
                role=str(public.get("role") or public.get("type") or "").strip(),
            )
        )
    return views


def select_job_instance_views(
    views: list[JobInstanceView],
    selectors: Sequence[str],
) -> list[JobInstanceView]:
    """Resolve `--instance` selectors, or fail instead of silently widening.

    ``rank=0`` and a bare ``0`` both address the same instance — the first is
    what the instance table prints, the second is what a person types. A role
    name (``worker``) selects every instance in that role, the way `hpc
    instances` roles do.
    """
    if not selectors:
        return list(views)

    selected: list[JobInstanceView] = []
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
            raise JobInstanceSelectionError(
                f"No job instance matches {selector!r}. Known instances: {known}. "
                "List them with `inspire job instances <job-name> --workspace <workspace>`."
            )
        for view in matches:
            if view.handle not in seen:
                seen.add(view.handle)
                selected.append(view)
    return selected
