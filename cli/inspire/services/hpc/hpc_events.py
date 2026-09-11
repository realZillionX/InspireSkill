"""HPC event projection shared by CLI and SDK."""

from __future__ import annotations
from typing import Any
from inspire.services.hpc.hpc_instances import HPCInstanceView

_COLLAPSE_FIELDS = (
    "object_id",
    "object_type",
    "reason",
    "message",
    "from",
    "first_timestamp",
    "last_timestamp",
)


def collapse_repeated_events(events: list[dict]) -> list[dict]:
    """Fold byte-identical occurrences into the existing ``count`` column.

    Both HPC event Actions return one row per raw occurrence and never
    populate ``count``: a pod with 20 distinct events answers with 106 rows,
    so a default ``--tail 20`` window can be spent on twenty copies of one
    ``BackOff``. Nothing is dropped — the multiplicity moves into the Count
    column that the shared renderer already has — and a row the platform only
    ever sent once is left untouched so it keeps rendering as before.
    """
    collapsed: dict[tuple[str, ...], dict[str, Any]] = {}
    occurrences: dict[tuple[str, ...], int] = {}
    order: list[tuple[str, ...]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        key = tuple(str(event.get(field) or "") for field in _COLLAPSE_FIELDS)
        if key not in collapsed:
            collapsed[key] = dict(event)
            occurrences[key] = 0
            order.append(key)
        try:
            occurrences[key] += int(str(event.get("count") or 1))
        except ValueError:
            occurrences[key] += 1

    rows: list[dict[str, Any]] = []
    for key in order:
        row = collapsed[key]
        if occurrences[key] > 1:
            row["count"] = occurrences[key]
        rows.append(row)
    return rows


def labelled_events(
    events: list[dict],
    views: list[HPCInstanceView],
) -> list[dict]:
    """Name each row with the identity `inspire hpc instances` prints.

    Several instances are concatenated into one timeline, and the only thing
    a row says about its origin is ``object_id`` — the namespaced pod handle,
    which `scrub_raw_ids` reduces to `<redacted>-cluster-slurmd-0` and which
    therefore never reaches output. Without the Role / Rank label attached
    here, the merged stream renders as one block in which no row can be traced
    back to the instance it came from.
    """
    labels = {view.handle: view.label for view in views}
    labels.update({view.pod: view.label for view in views})
    labelled: list[dict] = []
    for event in events:
        row = dict(event)
        label = labels.get(str(row.get("object_id") or "").strip())
        if label:
            row["instance"] = label
        labelled.append(row)
    return labelled
