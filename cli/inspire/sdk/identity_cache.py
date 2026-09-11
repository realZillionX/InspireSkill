"""Project shared identity snapshots into the full catalogues SDK callers need.

inspire.services.catalog.resource_index is authoritative only about its cached
observations, not current platform state. A fast-path hit needs a complete,
fresh scope, valid payloads and an unchanged snapshot token. CLI rows containing
only a name and ID cannot supply project permissions or image fields; they
trigger a live read instead of inventing those values.

Refreshes use inspire.services.catalog.resource_refresh and its leases. A busy
writer may leave a usable snapshot; otherwise readers go live without publishing
under somebody else's lease. Unavailable storage or missing stable identity also
falls back to the loader. Failed or partial quota enumeration must not become an
apparently complete empty catalogue.
"""

from __future__ import annotations

from collections.abc import Callable
import json
import sqlite3
import time
from typing import Any

from inspire.services.catalog.catalog_codec import encode_catalog, decode_catalog, validate_catalog
from inspire.services.catalog.resource_index import (
    ResourceIdentity, ResourceIndex, ResourceScope, scope_for_session, DEFAULT_TTL_SECONDS,
)
from inspire.services.catalog.resource_refresh import FetchResult, refresh_scope
from inspire.services.catalog.quota_cache import (
    CachedPricesLoader, fetch_quota_catalog, workload_for_schedule_type,
)
from .exceptions import ResolutionIncompleteError

IDENTITY_KINDS = frozenset({"workspaces", "projects", "compute_groups", "images", "prices"})
RESOURCE_TYPES = {
    "workspaces": "workspace", "projects": "project",
    "compute_groups": "compute-group", "images": "image",
}
CACHE_ERRORS = (OSError, TimeoutError, sqlite3.Error, ValueError, TypeError, KeyError)


def _identity(kind: str, row: Any) -> tuple[str, str]:
    fields = row if isinstance(row, dict) else vars(row)
    key = next((fields.get(k) for k in ("image_id", "project_id", "id", "logic_compute_group_id") if fields.get(k)), "")
    name = fields.get("name") or fields.get("logic_compute_group_name") or key
    if kind == "images":
        from inspire.services.catalog.images import image_label
        name = image_label(row)
    return str(key), str(name)


class IdentityCache:
    def __init__(self, account: str, ttl: float, base_url: str) -> None:
        self.account = account
        self.base_url = base_url
        self.ttl = ttl
        self._index: ResourceIndex | None = None

    def index(self) -> ResourceIndex | None:
        try:
            if self._index is None:
                self._index = ResourceIndex.for_account(self.account)
            return self._index
        except CACHE_ERRORS:
            return None

    def scope(self, session: object, kind: str, params: tuple[Any, ...]) -> ResourceScope | None:
        resource_type = RESOURCE_TYPES.get(kind, "")
        workspace = str(params[0] or "") if params else ""
        owner = ""
        if kind == "projects":
            # The filtered candidate set cannot reconcile the global catalog.
            owner = f"workspace:{workspace}" if workspace else ""
            workspace = ""
        elif kind == "images":
            owner = f"source:{params[1]}"
        elif kind == "prices":
            resource_type = "quota-" + workload_for_schedule_type(params[2])
            owner = "self"
        return scope_for_session(
            session, resource_type=resource_type, workspace_id=workspace, owner_scope=owner,
        )

    def _read(
        self, index: ResourceIndex, scope: ResourceScope, kind: str,
    ) -> tuple[list[Any] | None, bool]:
        """Return rows and whether unusable payloads need repair despite freshness."""
        token = index.snapshot_token(scope)
        interval = min(self.ttl, DEFAULT_TTL_SECONDS[scope.resource_type])
        if index.scope_due(scope, interval_seconds=int(interval), require_full=True):
            return None, False
        records = index.list_identities(scope, fresh_only=False)
        now = time.time()
        active = [row for row in records if row.tombstoned_at is None]
        if any(row.expires_at <= now or row.observed_at + self.ttl <= now for row in active):
            return None, False
        rows = []
        try:
            for record in active:
                if not record.payload:
                    return None, token == index.snapshot_token(scope)
                row = decode_catalog(json.loads(record.payload))
                key, name = _identity(kind, row)
                if key != record.resource_id or name != record.name:
                    return None, token == index.snapshot_token(scope)
                rows.append(row)
            validate_catalog(kind, rows)
        except (ValueError, TypeError, KeyError, AttributeError):
            return None, token == index.snapshot_token(scope)
        return (rows, False) if token == index.snapshot_token(scope) else (None, False)

    def get(
        self, session: object, kind: str, params: tuple[Any, ...], load: Callable[[], Any],
        *, groups: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> tuple[Any, bool]:
        index = self.index()
        scope = self.scope(session, kind, params)
        if index is None or scope is None:
            return load(), False
        if kind == "prices":
            return self._prices(index, scope, session, params, load, groups)
        refill = False
        try:
            rows, refill = self._read(index, scope, kind)
            if rows is not None:
                validate_catalog(kind, rows)
                return rows, True
        except CACHE_ERRORS:
            pass
        loaded: list[Any] = []
        errors: list[Exception] = []
        reused = False

        def fetch(_session: object, _workspace: str, _name: str) -> FetchResult:
            nonlocal reused
            try:
                # Another SDK writer may have repaired the scope before this lease.
                try:
                    value, _ = self._read(index, scope, kind)
                except CACHE_ERRORS:
                    value = None
                reused = value is not None
                if value is None:
                    value = load()
                validate_catalog(kind, value)
                loaded.append(value)
                records = []
                for row in value:
                    key, name = _identity(kind, row)
                    records.append(ResourceIdentity(
                        resource_id=str(key), name=str(name),
                        payload=json.dumps(encode_catalog(row), ensure_ascii=True),
                    ))
                return FetchResult(records)
            except Exception as error:
                errors.append(error)
                raise

        result = refresh_scope(
            index=index, session=session, scope=scope, resource_type=scope.resource_type,
            workspace_id=scope.workspace_id, workspace_name="", exact_name="", force=refill,
            fetcher=fetch,
        )
        if errors:
            raise errors[0]
        if loaded:
            return loaded[0], reused
        # A lease belongs to another writer. Reads may go live, but must never
        # publish without that lease or manufacture an authoritative empty set.
        if result.outcome == "busy":
            try:
                rows, _ = self._read(index, scope, kind)
                if rows is not None:
                    validate_catalog(kind, rows)
                    return rows, True
            except CACHE_ERRORS:
                pass
        return load(), False

    def _prices(
        self, index: ResourceIndex, scope: ResourceScope, session: Any,
        params: tuple[Any, ...], load: Callable[[], Any],
        groups: Callable[[], list[dict[str, Any]]] | None,
    ) -> tuple[Any, bool]:
        workspace, group, schedule = params
        loader = CachedPricesLoader(
            session=session, workspace_id=workspace, schedule_config_type=schedule,
            cache_index=index,
        )
        try:
            due = index.scope_due(scope, interval_seconds=int(self.ttl), require_full=True)
            if not due:
                token = index.snapshot_token(scope)
                records = index.list_identities(scope, fresh_only=False)
                if any(not row.fresh or row.observed_at + self.ttl <= time.time() for row in records):
                    return load(), False
                validate_catalog("prices", [json.loads(row.payload) for row in records])
                prices = loader(group)
                if token != index.snapshot_token(scope):
                    return load(), False
                if group in loader.served_from_cache:
                    return prices, True
                return prices, False
        except CACHE_ERRORS:
            return load(), False
        if groups is None:
            return load(), False
        catalog = []

        def fetch(_session: object, _workspace: str, _name: str) -> FetchResult:
            value = fetch_quota_catalog(
                session, workspace_id=workspace,
                workload=workload_for_schedule_type(schedule), groups=groups(),
            )
            catalog.append(value)
            return FetchResult(value.records, value.complete, value.error)

        refresh_scope(
            index=index, session=session, scope=scope, resource_type=scope.resource_type,
            workspace_id=workspace, workspace_name="", exact_name="", force=False, fetcher=fetch,
        )
        if catalog:
            if not catalog[0].complete:
                raise ResolutionIncompleteError(catalog[0].error)
            from inspire.services.catalog.quota_cache import prices_from_records
            return prices_from_records([r for r in catalog[0].records if r.owner_id == group]), False
        return load(), False

    def token(self, session: object, kind: str, params: tuple[Any, ...]) -> str:
        index = self.index()
        scope = self.scope(session, kind, params)
        if index is None or scope is None:
            return ""
        try:
            return str(index.snapshot_token(scope))
        except CACHE_ERRORS:
            return ""

    def invalidate_images(self) -> None:
        index = ResourceIndex.for_account(self.account)
        if index is not None:
            index.clear(["image"], base_url=self.base_url)

    def clear(self) -> None:
        index = ResourceIndex.for_account(self.account)
        if index is not None:
            index.clear([*RESOURCE_TYPES.values(), "quota-notebook", "quota-job", "quota-hpc", "quota-ray", "quota-serving"], base_url=self.base_url)
