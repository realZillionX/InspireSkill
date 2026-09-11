"""Small metadata store for catalogues absent from the shared identity index.

Only priority levels, fair-scheduling metadata and current-user data belong here.
Identities and prices use inspire.services.catalog.resource_index. Old identity blobs are
removed when the file is read. Values use a fixed codec, never pickle or imports
chosen by file contents. A generation check prevents a slow load from undoing a
concurrent clear, and unavailable storage falls back to live reads in
inspire.sdk.cache.
"""

from __future__ import annotations

from inspire.platform.web.flow import blocking_io

import json
import math
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

from inspire.accounts.cache_lock import exclusive_cache_lock
from inspire.accounts.storage import account_dir
from inspire.services.account.account_config import atomic_write_text

from inspire.services.catalog.catalog_codec import (
    encode_catalog as _encode,
    decode_catalog as _decode,
    validate_catalog as _validate_value,
)

KINDS = frozenset(
    {
        "priority_levels",
        "fair_scheduling",
        "current_user",
    }
)
MAX_ENTRIES = 256
MAX_BYTES = 8 * 1024 * 1024



def key_string(key: tuple[Any, ...]) -> str:
    return json.dumps(_encode(key), ensure_ascii=True, allow_nan=False, separators=(",", ":"))


class CatalogStore:
    def __init__(self, account: str, base_url: str) -> None:
        self.account = account
        self.base_url = base_url
        self.path: Path = account_dir(account, create=True) / "sdk-catalog-v1.json"

    def _read(self) -> dict[str, Any]:
        """Called under the stable sibling lock, including repair and pruning."""
        try:
            if self.path.stat().st_size > MAX_BYTES:
                raise ValueError("Oversized catalog")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                data["version"] != 1
                or data["account"] != self.account
                or not isinstance(data["generation"], str)
                or not isinstance(data["entries"], dict)
            ):
                raise ValueError("Invalid catalog envelope")
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            data = {"version": 1, "account": self.account, "generation": uuid4().hex, "entries": {}}
            self._write(data)
        now = time.time()
        changed = False
        for key, entry in list(data["entries"].items()):
            try:
                decoded_key = _decode(json.loads(key))
                if (
                    not isinstance(decoded_key, tuple)
                    or len(decoded_key) < 3
                    or decoded_key[0] not in KINDS
                    or decoded_key[1] != self.account
                    or not isinstance(decoded_key[2], str)
                    or not isinstance(entry["token"], str)
                    or not all(
                        type(entry[k]) in (int, float) and math.isfinite(entry[k])
                        for k in ("created", "expires")
                    )
                    or not entry["created"] <= now < entry["expires"]
                ):
                    raise ValueError("Invalid or expired entry")
                _validate_value(decoded_key[0], _decode(entry["value"]))
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
                del data["entries"][key]
                changed = True
        if changed:
            self._write(data)
        return data

    def _write(self, data: dict[str, Any]) -> None:
        entries = data["entries"]
        while len(entries) > MAX_ENTRIES:
            del entries[next(iter(entries))]
        while True:
            content = json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            if len(content) <= MAX_BYTES:
                break
            del entries[next(iter(entries))]
        atomic_write_text(self.path, content, private=True)

    @blocking_io
    def read(self, key: tuple[Any, ...]) -> tuple[str, dict[str, Any] | None]:
        with exclusive_cache_lock(self.path, timeout=5):
            data = self._read()
            return data["generation"], data["entries"].get(key_string(key))

    @blocking_io
    def put(
        self, key: tuple[Any, ...], value: Any, ttl: float, generation: str
    ) -> dict[str, Any] | None:
        _validate_value(key[0], value)
        encoded = _encode(value)
        with exclusive_cache_lock(self.path, timeout=5):
            data = self._read()
            # A mutation/clear during enumeration must not resurrect old data.
            if data["generation"] != generation:
                return None
            now = time.time()
            entry = {"created": now, "expires": now + ttl, "token": uuid4().hex, "value": encoded}
            key_text = key_string(key)
            data["entries"].pop(key_text, None)
            data["entries"][key_text] = entry
            self._write(data)
            return data["entries"].get(key_text)

    @blocking_io
    def invalidate(self, prefix: tuple[Any, ...] | None = None) -> None:
        with exclusive_cache_lock(self.path, timeout=5):
            data = self._read()
            for key_text in list(data["entries"]):
                key = _decode(json.loads(key_text))
                if (prefix is None and key[2] == self.base_url) or (
                    prefix is not None and key[: len(prefix)] == prefix
                ):
                    del data["entries"][key_text]
            data["generation"] = uuid4().hex
            self._write(data)

    @staticmethod
    def value(entry: dict[str, Any]) -> Any:
        return _decode(entry["value"])
