"""Resource availability ordering and public capacity views."""

from __future__ import annotations

import re

from inspire.services.utils.raw_ids import scrub_raw_ids


_REDACTED_ID_RE = re.compile(r"(?:\b[A-Za-z][A-Za-z0-9_-]*-)?(?:<redacted>|<[^<>]+-id>)")


def ordered_availability(availability: list) -> list:  # noqa: ANN401
    """Return the same decision order for Human and JSON output.

    GPU and CPU rows render as separate table sections.  Sorting only inside
    the Human formatter made JSON use platform enumeration order and, more
    importantly, applied the default output limit before ranking capacity.
    """
    gpu_rows = [a for a in availability if getattr(a, "resource_kind", "gpu") == "gpu"]
    cpu_rows = [a for a in availability if getattr(a, "resource_kind", "gpu") == "cpu"]
    gpu_rows.sort(
        # Workload defaults are high priority, so rank by the capacity they can
        # actually obtain after preemption; use the guarantee balance only as
        # the tiebreaker.
        key=lambda item: (item.high_priority_available_gpus, item.available_gpus),
        reverse=True,
    )
    cpu_rows.sort(key=lambda item: item.cpu_available, reverse=True)
    return [*gpu_rows, *cpu_rows]


def _public_metric(value: float | int) -> float:
    """Remove binary floating-point noise without hiding useful precision."""
    return round(float(value), 4)


def display_name(value: object, *, fallback: str = "-") -> str:
    text = _REDACTED_ID_RE.sub(" ", scrub_raw_ids(value))
    return " ".join(text.split()) or fallback


def public_availability_row(availability) -> dict[str, object]:  # noqa: ANN001
    row: dict[str, object] = {
        "workspace": display_name(
            getattr(availability, "workspace_name", ""),
            fallback="",
        ),
        "compute_group": display_name(getattr(availability, "group_name", "")),
        "kind": getattr(availability, "resource_kind", "gpu") or "gpu",
    }
    if row["kind"] == "cpu":
        row.update(
            {
                "cpu_total": _public_metric(availability.cpu_total),
                "cpu_used": _public_metric(availability.cpu_used),
                "cpu_available": _public_metric(availability.cpu_available),
                "memory_total_gib": _public_metric(availability.memory_total_gib),
                "memory_used_gib": _public_metric(availability.memory_used_gib),
                "memory_available_gib": _public_metric(availability.memory_available_gib),
            }
        )
        return row

    row.update(
        {
            "gpu_type": display_name(getattr(availability, "gpu_type", "")),
            "total_gpus": availability.total_gpus,
            "used_gpus": availability.used_gpus,
            "available_gpus": availability.available_gpus,
            "high_priority_available_gpus": availability.high_priority_available_gpus,
            "low_priority_gpus": availability.low_priority_gpus,
            "total_nodes": availability.total_nodes,
            "ready_nodes": availability.ready_nodes,
            "free_nodes": availability.free_nodes,
            "gpus_per_node": availability.gpu_per_node,
            "full_free_nodes": availability.full_free_nodes,
            "reclaimable_nodes": availability.reclaimable_nodes,
            "high_priority_free_nodes": availability.high_priority_free_nodes,
            "full_free_gpus": availability.full_free_gpus,
            "high_priority_free_gpus": availability.high_priority_free_gpus,
            "node_specs": [
                {
                    "node_type": spec.node_type,
                    "gpu_type": spec.gpu_type,
                    "gpu_count": spec.gpu_count,
                    "cpu_count": spec.cpu_count,
                    "memory_gib": spec.memory_gib,
                    "job_types": list(spec.job_types),
                }
                for spec in availability.node_specs
            ],
        }
    )
    return row
