"""Publish one resource scope for both CLI refreshes and SDK catalogue misses.

A fetcher reports completeness explicitly. Full scans may tombstone absent rows;
partial scans only merge, and exact-name scans cannot certify the whole scope.
The lease prevents competing publishers, while generation/revision checks stop a
slow fetch from restoring data invalidated during its request. A fresh or busy
outcome need not contain fetched rows: readers decide whether to reuse a snapshot
or go live. Network enumeration belongs in the supplied fetcher.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Mapping

from inspire.services.utils.raw_ids import scrub_raw_ids
from inspire.services.catalog.resource_index import (
    DEFAULT_TTL_SECONDS, ResourceIdentity, ResourceIndex, ResourceIndexDatabaseError,
    ResourceScope, StaleResourceIndexRefresh, scope_for_session,
)

@dataclass(frozen=True)
class FetchResult:
    """What one fetcher saw, and whether that was all of it.

    ``complete=False`` is the difference between "these are the rows" and
    "these are the rows I could read". Only the former may reconcile a scope
    and tombstone what it did not see; the latter merges, keeps the older
    rows, and carries ``error`` so the reason survives into ``cache status``.
    """

    records: list[ResourceIdentity]
    complete: bool = True
    error: str = ""


@dataclass(frozen=True)
class RefreshResult:
    resource_type: str
    workspace_name: str
    item_count: int
    outcome: str
    error: str = ""


@dataclass(frozen=True)
class RefreshSummary:
    results: list[RefreshResult]

    @property
    def error_count(self) -> int:
        return sum(result.outcome == "error" for result in self.results)

    @property
    def partial_count(self) -> int:
        """Scopes that cached what they could read and kept the rest.

        Separate from ``error_count``: nothing about the cache is broken and
        the previously cached rows are intact, so the command still succeeds.
        What the user needs to know is that the scope is not authoritative
        yet, which the printed summary and ``cache status`` both say.
        """
        return sum(result.outcome == "partial" for result in self.results)


Fetcher = Callable[[object, str, str], FetchResult]

def _record_refresh_error(
    index: ResourceIndex,
    scope: ResourceScope,
    error: str,
    *,
    attempted_at: float,
) -> None:
    """Record diagnostics without allowing a disposable cache to fail open."""
    try:
        index.record_refresh_error(scope, error, now=attempted_at)
    except (OSError, sqlite3.Error):
        pass


def _dedupe_records(records: Iterable[ResourceIdentity]) -> list[ResourceIdentity]:
    by_id: dict[str, ResourceIdentity] = {}
    for record in records:
        resource_id = str(record.resource_id or "").strip()
        name = str(record.name or "").strip()
        if not resource_id or not name:
            continue
        # `replace` rather than a fresh constructor: rebuilding field by field
        # silently drops whatever was added to ResourceIdentity since. The
        # remaining fields are stripped again on write in `_upsert_records`.
        by_id[resource_id] = replace(record, resource_id=resource_id, name=name)
    return list(by_id.values())


def refresh_scope(
    *,
    index: ResourceIndex,
    session: object,
    resource_type: str,
    workspace_id: str,
    workspace_name: str,
    exact_name: str,
    force: bool,
    fetcher: Fetcher,
    scope: ResourceScope | None = None,
    case_sensitive: bool | None = None,
    prefetched: FetchResult | None = None,
    prefetched_revision: int | None = None,
    prefetched_generation: int | None = None,
    prefetched_attempted_at: float | None = None,
    prefetched_child_revisions: Mapping[ResourceScope, int] | None = None,
) -> RefreshResult:
    scope = scope or scope_for_session(
        session,
        resource_type=resource_type,
        workspace_id=workspace_id,
        owner_scope="self" if resource_type not in {"workspace", "project", "compute-group"} else "",
    )
    if scope is None:
        return RefreshResult(
            resource_type=resource_type,
            workspace_name=workspace_name,
            item_count=0,
            outcome="error",
            error="The current account session has no stable identity.",
        )

    interval = DEFAULT_TTL_SECONDS.get(resource_type, 300)
    if not force and not exact_name:
        try:
            due = index.scope_due(
                scope,
                interval_seconds=interval,
                require_full=True,
            ) and index.attempt_due(scope, interval_seconds=interval)
        except (OSError, sqlite3.Error):
            due = True
        if not due:
            return RefreshResult(resource_type, workspace_name, 0, "fresh")

    try:
        lease = index.refresh_lease(scope, raise_on_error=True)
        with lease as acquired:
            if not acquired:
                return RefreshResult(resource_type, workspace_name, 0, "busy")
            try:
                attempted_at = (
                    float(prefetched_attempted_at)
                    if prefetched is not None and prefetched_attempted_at is not None
                    else time.time()
                )
                if (
                    prefetched is not None
                    and prefetched_revision is not None
                    and prefetched_generation is not None
                ):
                    expected_generation = prefetched_generation
                    expected_revision = prefetched_revision
                else:
                    expected_generation, expected_revision = index.snapshot_token(scope)
                child_revisions = prefetched_child_revisions
                if resource_type == "workspace" and not exact_name and prefetched is None:
                    try:
                        _, _, child_revisions = index.snapshot_workspace_refresh(scope)
                    except Exception:
                        child_revisions = None
                fetched = (
                    prefetched
                    if prefetched is not None
                    else fetcher(session, workspace_id, exact_name)
                )
                records = _dedupe_records(fetched.records)
                if not fetched.complete:
                    # Merge, never replace or reconcile: rows this pass could
                    # not see are rows it knows nothing about, not rows the
                    # platform removed. That holds for a `--name` refresh too
                    # -- the group that did not answer may be exactly the one
                    # holding that name. The scope stays short of a full
                    # refresh, so readers that demand one keep going live.
                    count = index.upsert(
                        scope,
                        records,
                        ttl_seconds=interval,
                        expected_revision=expected_revision,
                        expected_generation=expected_generation,
                        attempted_at=attempted_at,
                    )
                    if fetched.error:
                        _record_refresh_error(
                            index,
                            scope,
                            fetched.error,
                            attempted_at=attempted_at,
                        )
                    return RefreshResult(
                        resource_type,
                        workspace_name,
                        count,
                        "partial",
                        scrub_raw_ids(fetched.error),
                    )
                if exact_name:
                    count = index.replace_name(
                        scope,
                        exact_name,
                        records,
                        case_sensitive=case_sensitive,
                        ttl_seconds=interval,
                        expected_revision=expected_revision,
                        expected_generation=expected_generation,
                        attempted_at=attempted_at,
                    )
                else:
                    count = index.reconcile(
                        scope,
                        records,
                        ttl_seconds=interval,
                        expected_revision=expected_revision,
                        expected_generation=expected_generation,
                        attempted_at=attempted_at,
                    )
                if not exact_name:
                    # Cleanup must never change the outcome of a published snapshot.
                    if resource_type == "workspace" and child_revisions is not None:
                        try:
                            index.prune_orphan_workspace_scopes(
                                scope, (record.resource_id for record in records),
                                expected_generation=expected_generation,
                                expected_workspace_revision=expected_revision + 1,
                                expected_child_revisions=child_revisions,
                            )
                        except Exception:
                            pass
                    try:
                        index.purge_tombstones()
                    except Exception:
                        pass
                return RefreshResult(resource_type, workspace_name, count, "refreshed")
            except StaleResourceIndexRefresh:
                return RefreshResult(resource_type, workspace_name, 0, "stale")
            except (OSError, sqlite3.Error, ResourceIndexDatabaseError):
                return RefreshResult(
                    resource_type,
                    workspace_name,
                    0,
                    "error",
                    "The local resource name cache is unavailable.",
                )
            except Exception as exc:  # noqa: BLE001 - aggregate all scopes
                _record_refresh_error(
                    index,
                    scope,
                    str(exc),
                    attempted_at=attempted_at,
                )
                return RefreshResult(
                    resource_type,
                    workspace_name,
                    0,
                    "error",
                    scrub_raw_ids(str(exc) or type(exc).__name__),
                )
    except ResourceIndexDatabaseError:
        return RefreshResult(
            resource_type,
            workspace_name,
            0,
            "error",
            "The local resource name cache is unavailable.",
        )


__all__ = ["FetchResult", "RefreshResult", "RefreshSummary", "Fetcher", "refresh_scope"]
