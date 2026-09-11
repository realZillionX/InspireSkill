"""Catalogue snapshots with optional shared storage and per-client accounting.

RAM-only mode stores loader results for catalog_ttl. Disk mode routes identity
and price rows through inspire.sdk.identity_cache; only the remaining metadata
uses inspire.sdk.catalog_store. A shared read validates the disk snapshot before
counting a RAM hit, so a cache clear in another process is not ignored.

Loaders must reject incomplete results before returning: this layer does not
infer completeness from a list. Exceptions are never stored. Workload status,
logs and other live observations do not belong in this cache.
"""

from __future__ import annotations

from copy import deepcopy
from collections.abc import Callable
import math
import time
from typing import Any, TypeVar, cast

from inspire.platform.web.flow import blocking_call, perform_sync

from .exceptions import ValidationError
from .identity_cache import IdentityCache
from .catalog_store import CatalogStore, KINDS, MAX_ENTRIES

T = TypeVar("T")
CatalogKey = tuple[Any, ...]


class CatalogCache:
    def __init__(self, ttl: float = 60, *, store: CatalogStore | None = None) -> None:
        if (
            isinstance(ttl, bool)
            or not isinstance(ttl, (int, float))
            or not math.isfinite(ttl)
            or ttl < 0
        ):
            raise ValidationError("catalog_ttl must be finite non-negative seconds.")
        self.ttl = float(ttl)
        self._identity: IdentityCache | None = None
        self._identity_invalidation: IdentityCache | None = None
        self._store = store
        self._invalidation_store = store
        self._shared_hits = 0
        self._tokens: dict[CatalogKey, str] = {}
        self._entries: dict[CatalogKey, tuple[float, Any]] = {}
        self._hits = 0
        self._misses = 0

    def _get(self, key: CatalogKey, load: Callable[[], T]) -> T:
        if self._store is not None and self.ttl > 0 and key[0] in KINDS:
            return self._get_shared(key, load)
        entry = self._entries.get(key)
        if self.ttl > 0 and entry is not None and time.monotonic() < entry[0]:
            self._hits += 1
            return cast(T, deepcopy(entry[1]))
        self._entries.pop(key, None)
        self._misses += 1
        value = load()
        if self.ttl > 0:
            self._entries[key] = (time.monotonic() + self.ttl, deepcopy(value))
        return value

    def _get_shared(self, key: CatalogKey, load: Callable[[], T]) -> T:
        store = self._store
        assert store is not None
        if key[1:3] != (store.account, store.base_url):
            raise ValidationError("Catalog key belongs to another account or server.")
        self._make_room(key)
        generation = None
        try:
            generation, shared = store.read(key)
            if shared is not None:
                remaining = min(shared["expires"], shared["created"] + self.ttl) - time.time()
                if remaining > 0:
                    local = self._entries.get(key)
                    if (
                        local is not None
                        and time.monotonic() < local[0]
                        and self._tokens.get(key) == shared["token"]
                    ):
                        self._hits += 1
                        return cast(T, deepcopy(local[1]))
                    value = store.value(shared)
                    self._shared_hits += 1
                    self._entries[key] = (time.monotonic() + remaining, deepcopy(value))
                    self._tokens[key] = shared["token"]
                    return cast(T, value)
        except (OSError, TimeoutError, ValueError, TypeError):
            # An unavailable store is never permission to serve unchecked RAM.
            generation = None
        self._entries.pop(key, None)
        self._tokens.pop(key, None)
        self._misses += 1
        value = load()
        if generation is not None:
            try:
                shared = store.put(key, value, self.ttl, generation)
                if shared is not None:
                    self._entries[key] = (time.monotonic() + self.ttl, deepcopy(value))
                    self._tokens[key] = shared["token"]
            except (OSError, TimeoutError, ValueError, TypeError):
                pass
        return value

    def _make_room(self, key: CatalogKey) -> None:
        self.stats()
        while len(self._entries) >= MAX_ENTRIES and key not in self._entries:
            oldest = next(iter(self._entries))
            del self._entries[oldest]
            self._tokens.pop(oldest, None)
    def _get_identity(self, key: CatalogKey, session: Any, load: Callable[[], T], groups: Any) -> T:
        assert self._identity is not None
        self._make_room(key)
        value, shared = self._identity.get(session, key[0], key[3:], load, groups=groups)
        token = self._identity.token(session, key[0], key[3:])
        previous = self._entries.get(key)
        if shared:
            if previous is not None and time.monotonic() < previous[0] and self._tokens.get(key) == token:
                self._hits += 1
            else:
                self._shared_hits += 1
        else:
            self._misses += 1
        expiry = previous[0] if shared and previous is not None and self._tokens.get(key) == token else time.monotonic() + self.ttl
        self._entries[key] = (expiry, deepcopy(value))
        self._tokens[key] = token
        return cast(T, value)

    def _degrade(self) -> None:
        """Use live reads for this client after a failed invalidation fence."""
        self._entries.clear()
        self._tokens.clear()
        self.ttl = 0

    def clear(self) -> None:
        """Discard all snapshots; lifetime hit/miss counters are preserved."""
        self._entries.clear()
        self._tokens.clear()
        if self._identity is not None:
            self._identity.clear()
        if self._store is not None:
            self._store.invalidate()

    def _invalidate(self, kind: str, account: str, base_url: str, *scope: Any) -> None:
        """Discard a catalog kind, optionally restricted by a scope prefix."""
        if kind == "images" and self._identity_invalidation is not None:
            from inspire.services.catalog.resource_index import resource_index_path
            path = resource_index_path(account)
            if path is not None and perform_sync(blocking_call(path.exists)):
                self._identity_invalidation.invalidate_images()
        prefix = (kind, account, base_url, *scope)
        for key in list(self._entries):
            if key[: len(prefix)] == prefix:
                del self._entries[key]
                self._tokens.pop(key, None)
        disk = self._invalidation_store
        if disk is not None and (self._store is not None or perform_sync(blocking_call(disk.path.exists))):
            disk.invalidate(prefix)

    def stats(self) -> dict[str, int]:
        now = time.monotonic()
        for key, (expiry, _) in list(self._entries.items()):
            if self.ttl == 0 or expiry <= now:
                del self._entries[key]
                self._tokens.pop(key, None)
        result = {"hits": self._hits, "misses": self._misses, "entries": len(self._entries)}
        if self._store is not None:
            result["shared_hits"] = self._shared_hits
        return result
