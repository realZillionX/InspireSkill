"""Live refresh engine for the per-account resource identity index.

The index is disposable acceleration state. Every refresh reads the platform;
normal list/status commands continue to use live APIs as their source of truth.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from inspire.accounts import account_dir, current_account
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.resource_index import (
    DEFAULT_TTL_SECONDS,
    ResourceIdentity,
    ResourceIndex,
    ResourceIndexDatabaseError,
    GLOBAL_RESOURCE_TYPES,
    QUOTA_RESOURCE_TYPES,
    QUOTA_WORKLOADS,
    ResourceScope,
    StaleResourceIndexRefresh,
    quota_resource_type,
    scope_for_session,
)

RESOURCE_TYPES = (
    "workspace",
    "project",
    "compute-group",
    "image",
    "model",
    "job",
    "hpc",
    "ray",
    "serving",
    "notebook",
    "tensorboard",
    *QUOTA_RESOURCE_TYPES,
)
WORKSPACE_RESOURCE_TYPES = tuple(
    resource_type
    for resource_type in RESOURCE_TYPES
    if resource_type not in GLOBAL_RESOURCE_TYPES
)

# The scheduler must wake no slower than the shortest resource TTL, otherwise
# workload mappings sit expired between refreshes. It is also the floor on how
# often any account spawns a background refresh.
PERIODIC_REFRESH_INTERVAL_SECONDS = min(DEFAULT_TTL_SECONDS.values())
PERIODIC_REFRESH_STAMP = "resource-index-refresh.stamp"
PERIODIC_REFRESH_STAMP_MAX_AGE_SECONDS = 30 * 60

# A refresh reads whole scopes, not screens of them, so it pages at the bulk
# size rather than the UI's. At 100 a workspace holding ~1400 of the user's
# jobs cost 14 round trips every five minutes; the gateway caps `page_size` at
# `MAX_PAGE_SIZE` and clamps anything above it, so this only ever means fewer
# requests for the same rows.
REFRESH_PAGE_SIZE = 1000


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


def _filter_exact(
    records: Iterable[ResourceIdentity],
    exact_name: str,
    *,
    case_sensitive: bool = True,
) -> list[ResourceIdentity]:
    if not exact_name:
        return list(records)
    if case_sensitive:
        return [record for record in records if record.name == exact_name]
    needle = exact_name.casefold()
    return [record for record in records if record.name.casefold() == needle]


def _workspace_fetch(session: object, _workspace_id: str, exact_name: str) -> FetchResult:
    from inspire.platform.web.browser_api.workspaces import try_enumerate_workspaces

    live_items = try_enumerate_workspaces(session)  # type: ignore[arg-type]
    records = [
        ResourceIdentity(
            resource_id=str(item.get("id") or "").strip(),
            name=str(item.get("name") or "").strip(),
        )
        for item in live_items
        if isinstance(item, dict)
    ]
    return FetchResult(
        _filter_exact(
            _dedupe_records(records),
            exact_name,
            case_sensitive=False,
        ),
        complete=True,
    )


def _project_fetch(session: object, _workspace_id: str, exact_name: str) -> FetchResult:
    from inspire.platform.web.browser_api.projects import list_all_projects

    items = list_all_projects(session=session)  # type: ignore[arg-type]
    records = [
        ResourceIdentity(
            resource_id=item.project_id,
            name=item.name,
        )
        for item in items
    ]
    return FetchResult(
        _filter_exact(_dedupe_records(records), exact_name, case_sensitive=False)
    )


def _compute_group_fetch(
    session: object,
    workspace_id: str,
    exact_name: str,
) -> FetchResult:
    from inspire.platform.web.browser_api.availability.api import list_compute_groups

    items = list_compute_groups(workspace_id=workspace_id, session=session)  # type: ignore[arg-type]
    records = []
    for item in items:
        resource_id = str(
            item.get("logic_compute_group_id") or item.get("id") or ""
        ).strip()
        name = str(
            item.get("name")
            or item.get("logic_compute_group_name")
            or item.get("compute_group_name")
            or ""
        ).strip()
        records.append(ResourceIdentity(resource_id=resource_id, name=name))
    return FetchResult(
        _filter_exact(_dedupe_records(records), exact_name, case_sensitive=False)
    )


def _image_catalog(session: object, workspace_id: str) -> list[ResourceIdentity]:
    from inspire.platform.web.browser_api.notebooks import list_images

    records: list[ResourceIdentity] = []
    for source in ("SOURCE_OFFICIAL", "SOURCE_PUBLIC", "SOURCE_PRIVATE"):
        items = list_images(
            workspace_id=workspace_id,
            source=source,
            session=session,  # type: ignore[arg-type]
        )
        for item in items:
            name = str(item.name or "").strip()
            version = str(item.version or "").strip()
            label = name if ":" in name or not version else f"{name}:{version}"
            records.append(
                ResourceIdentity(
                    resource_id=item.image_id,
                    name=label,
                )
            )
    return _dedupe_records(records)


def _image_fetch(session: object, workspace_id: str, exact_name: str) -> FetchResult:
    return FetchResult(_filter_exact(_image_catalog(session, workspace_id), exact_name))


def _image_fetcher() -> Fetcher:
    """Build an image fetcher that reads each registry once, not each workspace.

    ``registry_hint: {workspace_id}`` names a registry, and workspaces share
    them: measured here, seven workspaces answer for ``qbHarbor`` and three
    国产卡 ones for ``sjHarbor``, with identical ``image_id`` sets inside each
    group. Fetching per workspace therefore downloaded the same ~5,400-image
    catalog seven times every cycle -- 42 MB of the 51 MB a full refresh moved,
    and 68 s of its 120 s.

    So each workspace is asked which registry it reads (one row, ~80 ms) and
    the catalog behind a registry already seen is reused verbatim. A workspace
    whose registry cannot be identified -- one with no publicly visible image
    at all -- is read on its own rather than assumed to match anyone.

    The memo lives for one refresh run. Across runs the scopes are what carry
    the answer forward, each with its own TTL.
    """
    by_registry: dict[str, list[ResourceIdentity]] = {}

    def _fetch(session: object, workspace_id: str, exact_name: str) -> FetchResult:
        from inspire.platform.web.browser_api.images import image_registry_id

        try:
            registry = image_registry_id(workspace_id, session=session)  # type: ignore[arg-type]
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:  # noqa: BLE001 - the probe is an optimization, not the read
            registry = ""
        if registry and registry in by_registry:
            return FetchResult(_filter_exact(by_registry[registry], exact_name))
        records = _image_catalog(session, workspace_id)
        if registry:
            by_registry[registry] = records
        return FetchResult(_filter_exact(records, exact_name))

    return _fetch


def _model_fetch(session: object, workspace_id: str, exact_name: str) -> FetchResult:
    from inspire.platform.web.browser_api.models import list_models

    records: list[ResourceIdentity] = []
    page = 1
    while True:
        items, total = list_models(
            workspace_id=workspace_id,
            keyword=exact_name or None,
            page=page,
            page_size=REFRESH_PAGE_SIZE,
            session=session,  # type: ignore[arg-type]
        )
        records.extend(
            ResourceIdentity(
                resource_id=item.model_id,
                name=item.name,
                owner_id=item.user_id,
                status=item.status,
                created_at=item.created_at,
            )
            for item in items
        )
        if not items or len(records) >= total or len(items) < REFRESH_PAGE_SIZE:
            break
        page += 1
    return FetchResult(_filter_exact(_dedupe_records(records), exact_name))


def _current_user_id(session: object) -> str:
    detail = getattr(session, "user_detail", None)
    if isinstance(detail, dict):
        value = detail.get("id") or detail.get("user_id")
        if value:
            return str(value).strip()

    from inspire.platform.web.browser_api.jobs import get_current_user

    detail = get_current_user(session=session)  # type: ignore[arg-type]
    value = detail.get("id") or detail.get("user_id")
    if not value:
        raise ValueError("Cannot determine the current account user.")
    try:
        setattr(session, "user_detail", detail)
        session.save(account=getattr(session, "account", None))  # type: ignore[attr-defined]
    except Exception:
        pass
    return str(value).strip()


def _job_fetch(session: object, workspace_id: str, exact_name: str) -> FetchResult:
    from inspire.platform.web.browser_api.jobs import list_jobs

    user_id = _current_user_id(session)
    records: list[ResourceIdentity] = []
    page = 1
    page_size = REFRESH_PAGE_SIZE
    while True:
        items, total = list_jobs(
            workspace_id=workspace_id,
            created_by=user_id,
            keyword=exact_name or None,
            page_num=page,
            page_size=page_size,
            session=session,  # type: ignore[arg-type]
        )
        records.extend(
            ResourceIdentity(
                resource_id=item.job_id,
                name=item.name,
                owner_id=item.created_by_id,
                status=item.status,
                created_at=item.created_at,
            )
            for item in items
        )
        if not items or len(records) >= total or len(items) < page_size:
            break
        page += 1
    return FetchResult(_filter_exact(_dedupe_records(records), exact_name))


def _tensorboard_fetch(
    session: object, workspace_id: str, exact_name: str
) -> FetchResult:
    from inspire.platform.web.browser_api.tensorboards import list_tensorboards

    user_id = _current_user_id(session)
    records: list[ResourceIdentity] = []
    page = 1
    page_size = REFRESH_PAGE_SIZE
    while True:
        items, total = list_tensorboards(
            workspace_id=workspace_id,
            created_by=user_id,
            keyword=exact_name or None,
            page_num=page,
            page_size=page_size,
            session=session,  # type: ignore[arg-type]
        )
        records.extend(
            ResourceIdentity(
                resource_id=item.tb_id,
                name=item.name,
                owner_id=user_id,
                status=item.status,
                created_at=item.created_at,
            )
            for item in items
            # A board may be created without a name; it can never be addressed
            # by one either, so caching it would only add a nameless row.
            if item.name
        )
        if not items or len(records) >= total or len(items) < page_size:
            break
        page += 1
    return FetchResult(_filter_exact(_dedupe_records(records), exact_name))


def _hpc_fetch(session: object, workspace_id: str, exact_name: str) -> FetchResult:
    from inspire.platform.web.browser_api.hpc_jobs import list_hpc_jobs

    user_id = _current_user_id(session)
    records: list[ResourceIdentity] = []
    page = 1
    page_size = REFRESH_PAGE_SIZE
    total_seen = 0
    while True:
        items, total = list_hpc_jobs(
            workspace_id=workspace_id,
            created_by=user_id,
            page_num=page,
            page_size=page_size,
            session=session,  # type: ignore[arg-type]
        )
        total_seen += len(items)
        records.extend(
            ResourceIdentity(
                resource_id=item.job_id,
                name=item.name,
                owner_id=item.created_by_id,
                status=item.status,
                created_at=item.created_at,
            )
            for item in items
        )
        if not items or total_seen >= total or len(items) < page_size:
            break
        page += 1
    return FetchResult(_filter_exact(_dedupe_records(records), exact_name))


def _ray_fetch(session: object, workspace_id: str, exact_name: str) -> FetchResult:
    from inspire.platform.web.browser_api.ray_jobs import list_ray_jobs

    user_id = _current_user_id(session)
    records: list[ResourceIdentity] = []
    page = 1
    page_size = REFRESH_PAGE_SIZE
    total_seen = 0
    while True:
        items, total = list_ray_jobs(
            workspace_id=workspace_id,
            user_ids=[user_id],
            page_num=page,
            page_size=page_size,
            session=session,  # type: ignore[arg-type]
        )
        total_seen += len(items)
        records.extend(
            ResourceIdentity(
                resource_id=item.ray_job_id,
                name=item.name,
                owner_id=item.created_by_id,
                status=item.status,
                created_at=item.created_at,
            )
            for item in items
        )
        if not items or total_seen >= total or len(items) < page_size:
            break
        page += 1
    return FetchResult(_filter_exact(_dedupe_records(records), exact_name))


def _serving_fetch(session: object, workspace_id: str, exact_name: str) -> FetchResult:
    from inspire.platform.web.browser_api.servings import list_servings

    records: list[ResourceIdentity] = []
    page = 1
    page_size = REFRESH_PAGE_SIZE
    total_seen = 0
    while True:
        items, total = list_servings(
            workspace_id=workspace_id,
            page=page,
            page_size=page_size,
            keyword=exact_name or None,
            session=session,  # type: ignore[arg-type]
        )
        total_seen += len(items)
        records.extend(
            ResourceIdentity(
                resource_id=item.inference_serving_id,
                name=item.name,
                status=item.status,
                created_at=item.created_at,
            )
            for item in items
        )
        if not items or total_seen >= total or len(items) < page_size:
            break
        page += 1
    return FetchResult(_filter_exact(_dedupe_records(records), exact_name))


def _notebook_fetch(session: object, workspace_id: str, exact_name: str) -> FetchResult:
    from inspire.cli.commands.notebook.notebook_lookup import (
        _list_notebooks_for_workspace,
        _notebook_compute_group,
        _notebook_id_from_item,
        _try_get_current_user_ids,
    )

    base_url = str(getattr(session, "base_url", None) or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("The current account session has no platform URL.")
    original_save = getattr(session, "save", None)
    account = str(getattr(session, "account", None) or "").strip() or None
    if callable(original_save) and account:
        def _save_to_refresh_account(*args: object, **kwargs: object) -> object:
            kwargs.setdefault("account", account)
            return original_save(*args, **kwargs)

        setattr(session, "save", _save_to_refresh_account)
    try:
        user_ids = _try_get_current_user_ids(
            session,  # type: ignore[arg-type]
            base_url=base_url,
        )
    finally:
        if callable(original_save) and account:
            setattr(session, "save", original_save)
    if not user_ids:
        raise ValueError("Cannot determine the current account user.")
    items = _list_notebooks_for_workspace(
        session,  # type: ignore[arg-type]
        workspace_id=workspace_id,
        user_ids=user_ids,
        keyword=exact_name,
    )
    records = [
        ResourceIdentity(
            resource_id=str(_notebook_id_from_item(item) or ""),
            name=str(item.get("name") or ""),
            owner_id=str(
                item.get("user_id")
                or item.get("owner_id")
                or item.get("creator_id")
                or ""
            ),
            status=str(item.get("status") or ""),
            created_at=str(item.get("created_at") or ""),
            compute_group=_notebook_compute_group(item),
        )
        for item in items
    ]
    return FetchResult(_filter_exact(_dedupe_records(records), exact_name))


def _quota_fetcher(workload: str) -> Fetcher:
    """Build the fetcher for one workload's quota catalog.

    The scope is a whole workspace catalog, so this fans out over every
    compute group. That is the same 1+N the lazy path would pay on a cold
    cache, done once for everything instead of once per group asked about.

    A fan-out that wide meets the platform's rate limiter, so it reports
    partial results as partial: a group that did not answer must never be
    cached as a group with no quotas.
    """

    def _fetch(session: object, workspace_id: str, exact_name: str) -> FetchResult:
        from inspire.cli.utils.quota_cache import fetch_quota_catalog

        catalog = fetch_quota_catalog(
            session,
            workspace_id=workspace_id,
            workload=workload,
        )
        # Quota names repeat across compute groups on purpose; that ambiguity
        # is what `--group` disambiguates, so records are not deduped by name.
        return FetchResult(
            _filter_exact(catalog.records, exact_name),
            complete=catalog.complete,
            error=catalog.error,
        )

    return _fetch


RESOURCE_FETCHERS: Mapping[str, Fetcher] = {
    "workspace": _workspace_fetch,
    "project": _project_fetch,
    "compute-group": _compute_group_fetch,
    "image": _image_fetch,
    "model": _model_fetch,
    "job": _job_fetch,
    "hpc": _hpc_fetch,
    "ray": _ray_fetch,
    "serving": _serving_fetch,
    "notebook": _notebook_fetch,
    "tensorboard": _tensorboard_fetch,
    **{
        quota_resource_type(workload): _quota_fetcher(workload)
        for workload in QUOTA_WORKLOADS
    },
}


def _workspace_names(
    session: object,
    *,
    workspace_fetch: FetchResult,
) -> dict[str, str]:
    names = {
        record.resource_id: record.name
        for record in workspace_fetch.records
        if record.resource_id and record.name
    }
    cached = getattr(session, "all_workspace_names", None)
    if not workspace_fetch.complete and isinstance(cached, dict):
        for workspace_id, name in cached.items():
            if workspace_id and name:
                names.setdefault(str(workspace_id), str(name))
    return names


def _cached_workspace_names(
    session: object,
    *,
    index: ResourceIndex,
    workspace_scope: ResourceScope | None,
) -> dict[str, str]:
    names: dict[str, str] = {}
    if workspace_scope is not None:
        try:
            names.update(
                {
                    item.resource_id: item.name
                    for item in index.list_identities(workspace_scope)
                    if item.resource_id and item.name
                }
            )
        except (OSError, sqlite3.Error):
            pass
    cached = getattr(session, "all_workspace_names", None)
    if isinstance(cached, dict):
        for workspace_id, name in cached.items():
            if workspace_id and name:
                names.setdefault(str(workspace_id), str(name))
    return names


def _workspace_bound_scope(
    session: object,
    *,
    resource_type: str,
    workspace_id: str,
) -> ResourceScope | None:
    return scope_for_session(
        session,
        resource_type=resource_type,
        workspace_id=workspace_id,
        owner_scope=(
            "self"
            if resource_type not in {"workspace", "project", "compute-group"}
            else ""
        ),
    )


def _all_selected_scopes_fresh(
    *,
    session: object,
    index: ResourceIndex,
    resource_types: Sequence[str],
    workspace_ids: Sequence[str],
) -> bool:
    if not resource_types or not workspace_ids:
        return False
    try:
        for resource_type in resource_types:
            interval = DEFAULT_TTL_SECONDS.get(resource_type, 300)
            for workspace_id in workspace_ids:
                scope = _workspace_bound_scope(
                    session,
                    resource_type=resource_type,
                    workspace_id=workspace_id,
                )
                if scope is None or index.scope_due(
                    scope,
                    interval_seconds=interval,
                    require_full=True,
                ):
                    return False
    except (OSError, sqlite3.Error):
        return False
    return True


def _select_workspace_ids(
    workspace_names: Mapping[str, str],
    requested: Sequence[str] | None,
) -> list[str]:
    if not requested or any(value.strip().lower() == "all" for value in requested):
        return list(workspace_names)

    selected: list[str] = []
    for requested_name in requested:
        normalized = requested_name.strip().casefold()
        matches = [
            workspace_id
            for workspace_id, name in workspace_names.items()
            if name.strip().casefold() == normalized
        ]
        if not matches:
            raise ValueError(f"Unknown workspace name: {requested_name!r}.")
        if len(matches) > 1:
            raise ValueError(f"Workspace name is ambiguous: {requested_name!r}.")
        if matches[0] not in selected:
            selected.append(matches[0])
    return selected


def _refresh_one(
    *,
    index: ResourceIndex,
    session: object,
    resource_type: str,
    workspace_id: str,
    workspace_name: str,
    exact_name: str,
    force: bool,
    fetcher: Fetcher,
    prefetched: FetchResult | None = None,
    prefetched_revision: int | None = None,
    prefetched_generation: int | None = None,
    prefetched_attempted_at: float | None = None,
) -> RefreshResult:
    scope = scope_for_session(
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


def refresh_resource_index(
    *,
    session: object,
    index: ResourceIndex,
    resource_types: Sequence[str] | None = None,
    workspace_names: Sequence[str] | None = None,
    exact_name: str = "",
    force: bool = False,
    fetchers: Mapping[str, Fetcher] | None = None,
) -> RefreshSummary:
    """Refresh selected resource scopes and return a name-only summary."""
    selected_types = tuple(resource_types or RESOURCE_TYPES)
    unknown = sorted(set(selected_types) - set(RESOURCE_TYPES))
    if unknown:
        raise ValueError(f"Unknown resource type: {', '.join(unknown)}")
    if exact_name and len(selected_types) != 1:
        raise ValueError("--name requires exactly one --resource.")

    if fetchers is None:
        # A fresh image fetcher per run: its registry memo must not outlive the
        # refresh that filled it, or a second run would reuse a catalog nobody
        # re-read.
        registry: Mapping[str, Fetcher] = {**RESOURCE_FETCHERS, "image": _image_fetcher()}
    else:
        registry = fetchers
    workspace_fetcher = registry["workspace"]
    workspace_scope = scope_for_session(session, resource_type="workspace")
    workspace_types = tuple(
        resource_type
        for resource_type in selected_types
        if resource_type in WORKSPACE_RESOURCE_TYPES
    )
    cached_names = _cached_workspace_names(
        session,
        index=index,
        workspace_scope=workspace_scope,
    )
    try:
        cached_workspace_ids = _select_workspace_ids(cached_names, workspace_names)
    except ValueError:
        cached_workspace_ids = []

    workspace_due = True
    if workspace_scope is not None:
        try:
            workspace_due = index.scope_due(
                workspace_scope,
                interval_seconds=DEFAULT_TTL_SECONDS["workspace"],
                require_full=True,
            )
        except (OSError, sqlite3.Error):
            workspace_due = True

    selected_workspace_is_fresh = (
        not force
        and not exact_name
        and "workspace" not in selected_types
        and bool(cached_workspace_ids)
        and _all_selected_scopes_fresh(
            session=session,
            index=index,
            resource_types=workspace_types,
            workspace_ids=cached_workspace_ids,
        )
    )
    needs_workspace_fetch = (
        ("workspace" in selected_types and (force or exact_name or workspace_due))
        or (
            bool(workspace_types)
            and not selected_workspace_is_fresh
            and (force or workspace_due or not cached_workspace_ids)
        )
    )

    workspace_snapshot = FetchResult(
        [
            ResourceIdentity(resource_id=workspace_id, name=name)
            for workspace_id, name in cached_names.items()
        ],
        complete=False,
    )
    workspace_revision: int | None = None
    workspace_generation: int | None = None
    workspace_child_revisions: dict[ResourceScope, int] = {}
    workspace_attempted_at = time.time()
    workspace_error = ""
    workspace_fetched = False
    if needs_workspace_fetch:
        try:
            if workspace_scope is not None:
                (
                    workspace_generation,
                    workspace_revision,
                    workspace_child_revisions,
                ) = index.snapshot_workspace_refresh(workspace_scope)
            workspace_attempted_at = time.time()
            workspace_snapshot = workspace_fetcher(
                session,
                "",
                exact_name if selected_types == ("workspace",) else "",
            )
            workspace_fetched = True
        except (OSError, sqlite3.Error, ResourceIndexDatabaseError):
            workspace_error = "The local resource name cache is unavailable."
        except Exception as exc:
            workspace_error = str(exc) or type(exc).__name__

    names_by_id = (
        {}
        if workspace_error
        else _workspace_names(session, workspace_fetch=workspace_snapshot)
    )
    selected_workspace_ids = (
        []
        if workspace_error
        else _select_workspace_ids(names_by_id, workspace_names)
    )

    results: list[RefreshResult] = []
    for resource_type in selected_types:
        fetcher = registry[resource_type]
        if resource_type == "workspace":
            if workspace_error:
                if workspace_scope is not None:
                    _record_refresh_error(
                        index,
                        workspace_scope,
                        workspace_error,
                        attempted_at=workspace_attempted_at,
                    )
                results.append(
                    RefreshResult(
                        "workspace",
                        "",
                        0,
                        "error",
                        scrub_raw_ids(workspace_error),
                    )
                )
                continue
            workspace_result = _refresh_one(
                index=index,
                session=session,
                resource_type="workspace",
                workspace_id="",
                workspace_name="",
                exact_name=exact_name,
                force=force,
                fetcher=workspace_fetcher,
                prefetched=workspace_snapshot if workspace_fetched else None,
                prefetched_revision=workspace_revision,
                prefetched_generation=workspace_generation,
                prefetched_attempted_at=workspace_attempted_at,
            )
            if (
                workspace_result.outcome == "refreshed"
                and workspace_fetched
                and workspace_snapshot.complete
                and not exact_name
                and workspace_scope is not None
                and workspace_generation is not None
                and workspace_revision is not None
            ):
                try:
                    index.prune_orphan_workspace_scopes(
                        workspace_scope,
                        names_by_id,
                        expected_generation=workspace_generation,
                        expected_workspace_revision=workspace_revision + 1,
                        expected_child_revisions=workspace_child_revisions,
                    )
                except StaleResourceIndexRefresh:
                    workspace_result = RefreshResult(
                        "workspace",
                        "",
                        workspace_result.item_count,
                        "stale",
                    )
                except (OSError, sqlite3.Error):
                    workspace_result = RefreshResult(
                        "workspace",
                        "",
                        workspace_result.item_count,
                        "error",
                        "The local resource name cache is unavailable.",
                    )
            results.append(workspace_result)
            continue

        # "workspace" already returned above; the rest of GLOBAL_RESOURCE_TYPES
        # refreshes once, without a workspace fan-out.
        if resource_type in GLOBAL_RESOURCE_TYPES:
            results.append(
                _refresh_one(
                    index=index,
                    session=session,
                    resource_type=resource_type,
                    workspace_id="",
                    workspace_name="",
                    exact_name=exact_name,
                    force=force,
                    fetcher=fetcher,
                )
            )
            continue

        if workspace_error:
            results.append(
                RefreshResult(
                    resource_type,
                    "",
                    0,
                    "error",
                    scrub_raw_ids(workspace_error),
                )
            )
            continue

        if not selected_workspace_ids:
            results.append(
                RefreshResult(
                    resource_type,
                    "",
                    0,
                    "error",
                    "No visible workspace names are available.",
                )
            )
            continue
        for workspace_id in selected_workspace_ids:
            results.append(
                _refresh_one(
                    index=index,
                    session=session,
                    resource_type=resource_type,
                    workspace_id=workspace_id,
                    workspace_name=names_by_id.get(workspace_id, ""),
                    exact_name=exact_name,
                    force=force,
                    fetcher=fetcher,
                )
            )

    try:
        index.purge_tombstones()
    except (OSError, sqlite3.Error):
        pass
    return RefreshSummary(results)


def periodic_refresh_stamp_path(account: str | None = None) -> Path | None:
    selected = str(account or "").strip() or current_account()
    if not selected:
        return None
    return account_dir(selected) / PERIODIC_REFRESH_STAMP


def maybe_spawn_periodic_refresh(
    *,
    interval_seconds: int = PERIODIC_REFRESH_INTERVAL_SECONDS,
) -> bool:
    """Spawn a quiet due-only refresh when a valid cached session is available."""
    if os.environ.get("INSPIRE_RESOURCE_INDEX_REFRESH_CHILD") == "1":
        return False
    if os.environ.get("INSPIRE_DISABLE_RESOURCE_INDEX_REFRESH") == "1":
        return False
    if "PYTEST_CURRENT_TEST" in os.environ:
        return False

    account = current_account()
    stamp = periodic_refresh_stamp_path(account)
    if not account or stamp is None:
        return False

    from inspire.platform.web.session.models import WebSession

    if WebSession.load(account=account) is None:
        return False

    now = time.time()
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(
                    stamp,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                try:
                    stamp.chmod(0o600)
                    contents = stamp.read_text(encoding="ascii").strip()
                    pid = int(contents)
                    stamp_age = max(0.0, now - stamp.stat().st_mtime)
                except (OSError, ValueError):
                    pid = 0
                    try:
                        stamp_age = max(0.0, now - stamp.stat().st_mtime)
                    except OSError:
                        stamp_age = 0.0
                if pid and stamp_age < PERIODIC_REFRESH_STAMP_MAX_AGE_SECONDS:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        return False
                    except OSError:
                        return False
                    else:
                        return False
                try:
                    if (
                        stamp_age < PERIODIC_REFRESH_STAMP_MAX_AGE_SECONDS
                        and stamp_age < max(30, interval_seconds)
                    ):
                        return False
                    stamp.unlink()
                except FileNotFoundError:
                    continue
                except OSError:
                    return False
                continue
            else:
                with os.fdopen(fd, "w", encoding="ascii") as handle:
                    handle.write(str(os.getpid()))
                break
        else:
            return False
    except OSError:
        return False

    env = os.environ.copy()
    env["INSPIRE_RESOURCE_INDEX_REFRESH_CHILD"] = "1"
    env["INSPIRE_SKIP_UPDATE_CHECK"] = "1"
    env["INSPIRE_RESOURCE_INDEX_REFRESH_ACCOUNT"] = account
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "inspire.cli.main",
                "cache",
                "refresh",
                "--due",
                "--quiet",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        child_pid = getattr(process, "pid", None)
        if isinstance(child_pid, int) and child_pid > 0:
            try:
                stamp.write_text(str(child_pid), encoding="ascii")
                stamp.chmod(0o600)
            except OSError:
                pass
    except OSError:
        try:
            stamp.unlink()
        except OSError:
            pass
        return False
    return True


__all__ = [
    "GLOBAL_RESOURCE_TYPES",
    "PERIODIC_REFRESH_INTERVAL_SECONDS",
    "PERIODIC_REFRESH_STAMP_MAX_AGE_SECONDS",
    "RESOURCE_FETCHERS",
    "RESOURCE_TYPES",
    "WORKSPACE_RESOURCE_TYPES",
    "FetchResult",
    "RefreshResult",
    "RefreshSummary",
    "maybe_spawn_periodic_refresh",
    "periodic_refresh_stamp_path",
    "refresh_resource_index",
]
