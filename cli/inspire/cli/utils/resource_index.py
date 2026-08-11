"""Disposable per-account resource identity index.

The platform remains the sole source of truth.  This SQLite database only
accelerates name-to-handle resolution; it must never become the backing store
for normal ``list`` or status output.

Quota rows live here too, because a quota *is* a name-to-handle mapping: the
user-facing name is the ``gpu,cpu,mem`` triple and the handle is the platform
``quota_id``. They carry their compute group in ``compute_group`` and the raw
price object in ``payload``.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

from inspire.accounts import account_dir, current_account

SCHEMA_VERSION = 4
RESOURCE_INDEX_FILENAME = "resource-index.sqlite3"

# Two tiers. Workloads come and go under the user's own hand, so they carry the
# shorter TTL; platform catalog data only moves when an admin changes something.
# The shortest value here also paces the background refresh, so lowering it
# costs a background process per account that often.
DEFAULT_TTL_SECONDS: dict[str, int] = {
    "workspace": 30 * 60,
    "project": 30 * 60,
    "compute-group": 30 * 60,
    "image": 30 * 60,
    "model": 30 * 60,
    "job": 5 * 60,
    "hpc": 5 * 60,
    "ray": 5 * 60,
    "serving": 5 * 60,
    "notebook": 5 * 60,
}

# One resource type per workload: a compute group exposes a different quota
# catalog per schedule config type, and each needs its own refresh cadence and
# its own complete-scope marker.
QUOTA_WORKLOADS: tuple[str, ...] = ("notebook", "job", "hpc", "ray", "serving")
QUOTA_RESOURCE_TYPES: tuple[str, ...] = tuple(
    f"quota-{workload}" for workload in QUOTA_WORKLOADS
)

DEFAULT_TTL_SECONDS.update(
    {resource_type: 30 * 60 for resource_type in QUOTA_RESOURCE_TYPES}
)


def quota_resource_type(workload: str) -> str:
    return f"quota-{str(workload or '').strip().lower()}"


CASE_INSENSITIVE_RESOURCE_TYPES = frozenset(
    {"workspace", "project", "compute-group"}
)

# Resource kinds that are not scoped to a workspace. A project in particular
# belongs to several workspaces at once (``ProjectInfo.workspace_ids``), so
# every scope for these types must use an empty workspace id -- otherwise a
# refresh and a lookup can disagree about where a name lives.
GLOBAL_RESOURCE_TYPES = frozenset({"workspace", "project"})


def scope_workspace_id(resource_type: str, workspace_id: str) -> str:
    """Blank the workspace for globally scoped resource kinds."""
    if str(resource_type or "").strip().lower() in GLOBAL_RESOURCE_TYPES:
        return ""
    return workspace_id


class StaleResourceIndexRefresh(RuntimeError):
    """Raised when a refresh snapshot lost a race with a newer cache mutation."""


class ResourceIndexDatabaseError(RuntimeError):
    """Raised when the disposable SQLite index is unavailable."""


class _CorruptResourceIndexError(sqlite3.DatabaseError):
    """Internal marker for a failed SQLite integrity check."""


@dataclass(frozen=True)
class ResourceScope:
    """Stable namespace for one name-resolution candidate set."""

    base_url: str
    subject_id: str
    resource_type: str
    workspace_id: str = ""
    owner_scope: str = ""

    def normalized(self) -> "ResourceScope":
        return ResourceScope(
            base_url=str(self.base_url or "").strip().rstrip("/"),
            subject_id=str(self.subject_id or "").strip(),
            resource_type=str(self.resource_type or "").strip().lower(),
            workspace_id=str(self.workspace_id or "").strip(),
            owner_scope=str(self.owner_scope or "").strip(),
        )

    def validate(self) -> "ResourceScope":
        scope = self.normalized()
        if not scope.base_url:
            raise ValueError("resource index scope requires base_url")
        if not scope.subject_id:
            raise ValueError("resource index scope requires subject_id")
        if not scope.resource_type:
            raise ValueError("resource index scope requires resource_type")
        return scope

    def lease_key(self) -> str:
        scope = self.validate()
        return "\x1f".join(
            (
                scope.base_url,
                scope.subject_id,
                scope.resource_type,
                scope.workspace_id,
                scope.owner_scope,
            )
        )


@dataclass(frozen=True)
class ResourceIdentity:
    """Minimal identity metadata retained by the local index."""

    resource_id: str
    name: str
    owner_id: str = ""
    status: str = ""
    created_at: str = ""
    # Compute group name, cached for the types whose transport or scheduling
    # decisions read it (notebook SSH policy, quota rows). Empty elsewhere.
    compute_group: str = ""
    # Opaque JSON text for types whose consumers need the platform payload
    # verbatim (quota rows carry the price object `create` has to echo back).
    # The index never interprets it. Empty elsewhere.
    payload: str = ""
    observed_at: float = 0.0
    expires_at: float = 0.0
    tombstoned_at: float | None = None

    @property
    def fresh(self) -> bool:
        return self.tombstoned_at is None and self.expires_at > time.time()


@dataclass(frozen=True)
class ScopeStatus:
    resource_type: str
    workspace_id: str
    active_count: int
    last_refresh_at: float
    last_full_refresh_at: float
    last_error: str



def resource_index_path(account: str | None = None) -> Path | None:
    """Return the selected account's index path, or ``None`` without an account."""
    selected = str(account or "").strip() or current_account()
    if not selected:
        return None
    return account_dir(selected) / RESOURCE_INDEX_FILENAME


def _subject_from_session(session: object) -> str:
    detail = getattr(session, "user_detail", None)
    if isinstance(detail, Mapping):
        value = detail.get("id") or detail.get("user_id") or detail.get("uid")
        if value:
            return str(value).strip()
    username = str(getattr(session, "login_username", None) or "").strip()
    return f"login:{username}" if username else ""


def scope_for_session(
    session: object,
    *,
    resource_type: str,
    workspace_id: str = "",
    owner_scope: str = "",
    subject_id: str = "",
    base_url: str = "",
) -> ResourceScope | None:
    """Build a cache scope from a live session.

    Missing stable account identity disables caching rather than risking
    cross-account reuse.
    """
    resolved_base_url = (
        str(base_url or "").strip()
        or str(getattr(session, "base_url", None) or "").strip()
    )
    resolved_subject = str(subject_id or "").strip() or _subject_from_session(session)
    if not resolved_base_url or not resolved_subject:
        return None
    return ResourceScope(
        base_url=resolved_base_url,
        subject_id=resolved_subject,
        resource_type=resource_type,
        workspace_id=workspace_id,
        owner_scope=owner_scope,
    ).validate()


class ResourceIndex:
    """SQLite-backed, concurrency-safe identity index."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        try:
            self._initialize()
        except sqlite3.DatabaseError as exc:
            if not self._is_corruption_error(exc):
                raise
            self._discard_corrupt_database()
            self._initialize()

    @classmethod
    def for_account(cls, account: str | None = None) -> "ResourceIndex | None":
        path = resource_index_path(account)
        return cls(path) if path is not None else None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS resource_identity (
                    base_url TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    owner_scope TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    owner_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT '',
                    compute_group TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL DEFAULT '',
                    observed_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    last_seen_scan_id TEXT,
                    tombstoned_at REAL,
                    PRIMARY KEY (
                        base_url,
                        subject_id,
                        resource_type,
                        workspace_id,
                        owner_scope,
                        resource_id
                    )
                );

                CREATE INDEX IF NOT EXISTS resource_identity_name_lookup
                ON resource_identity (
                    base_url,
                    subject_id,
                    resource_type,
                    workspace_id,
                    owner_scope,
                    name,
                    tombstoned_at,
                    expires_at
                );

                CREATE TABLE IF NOT EXISTS resource_scope (
                    base_url TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    owner_scope TEXT NOT NULL,
                    last_attempt_at REAL NOT NULL DEFAULT 0,
                    last_refresh_at REAL NOT NULL DEFAULT 0,
                    last_full_refresh_at REAL NOT NULL DEFAULT 0,
                    last_scan_id TEXT,
                    refresh_complete INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    mutation_revision INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (
                        base_url,
                        subject_id,
                        resource_type,
                        workspace_id,
                        owner_scope
                    )
                );

                CREATE TABLE IF NOT EXISTS refresh_lease (
                    lease_key TEXT PRIMARY KEY,
                    holder TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );

                -- Quota rows briefly lived in their own table before becoming
                -- ordinary resource_identity rows. Reclaim the space.
                DROP TABLE IF EXISTS quota_price;
                """
            )
            identity_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(resource_identity)"
                ).fetchall()
            }
            if "compute_group" not in identity_columns:
                connection.execute(
                    """
                    ALTER TABLE resource_identity
                    ADD COLUMN compute_group TEXT NOT NULL DEFAULT ''
                    """
                )
            if "payload" not in identity_columns:
                connection.execute(
                    """
                    ALTER TABLE resource_identity
                    ADD COLUMN payload TEXT NOT NULL DEFAULT ''
                    """
                )
            scope_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(resource_scope)").fetchall()
            }
            if "last_attempt_at" not in scope_columns:
                connection.execute(
                    """
                    ALTER TABLE resource_scope
                    ADD COLUMN last_attempt_at REAL NOT NULL DEFAULT 0
                    """
                )
            if "mutation_revision" not in scope_columns:
                connection.execute(
                    """
                    ALTER TABLE resource_scope
                    ADD COLUMN mutation_revision INTEGER NOT NULL DEFAULT 0
                    """
                )
            # A resource kind this build no longer knows can never be refreshed,
            # never be named by `cache clear --resource`, and still shows up in
            # `cache status` — `ssh-key` outlived its commands that way. Drop the
            # rows so the index only reports kinds that still exist.
            known = sorted(DEFAULT_TTL_SECONDS)
            placeholders = ",".join("?" for _ in known)
            for table in ("resource_identity", "resource_scope"):
                connection.execute(
                    f"DELETE FROM {table} WHERE resource_type NOT IN ({placeholders})",
                    known,
                )
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES('cache_generation', '0')
                ON CONFLICT(key) DO NOTHING
                """
            )
            integrity = connection.execute("PRAGMA quick_check(1)").fetchone()
            if integrity is None or str(integrity[0] or "").strip().lower() != "ok":
                detail = str(integrity[0] if integrity is not None else "unknown error")
                raise _CorruptResourceIndexError(detail)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _is_corruption_error(error: BaseException) -> bool:
        code = getattr(error, "sqlite_errorcode", None)
        if isinstance(code, int):
            primary_code = code & 0xFF
            corruption_codes = {
                value
                for value in (
                    getattr(sqlite3, "SQLITE_CORRUPT", None),
                    getattr(sqlite3, "SQLITE_NOTADB", None),
                )
                if isinstance(value, int)
            }
            if primary_code in corruption_codes:
                return True
        message = str(error).casefold()
        return any(
            marker in message
            for marker in (
                "database disk image is malformed",
                "file is not a database",
                "malformed database schema",
            )
        )

    def _discard_corrupt_database(self) -> None:
        """Delete the disposable cache and any sidecars from a failed check.

        Resource identities are only an acceleration layer. Keeping a copy of
        a corrupt database creates local debris without preserving source-of-
        truth data, so recovery starts from an empty index.
        """
        for source in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            try:
                source.unlink(missing_ok=True)
            except OSError as exc:
                raise OSError(
                    f"Could not discard corrupt resource index: {source}"
                ) from exc

    @staticmethod
    def _scope_values(scope: ResourceScope) -> tuple[str, str, str, str, str]:
        value = scope.validate()
        return (
            value.base_url,
            value.subject_id,
            value.resource_type,
            value.workspace_id,
            value.owner_scope,
        )

    @staticmethod
    def _row_identity(row: sqlite3.Row) -> ResourceIdentity:
        return ResourceIdentity(
            resource_id=str(row["resource_id"]),
            name=str(row["name"]),
            owner_id=str(row["owner_id"] or ""),
            status=str(row["status"] or ""),
            created_at=str(row["created_at"] or ""),
            compute_group=str(row["compute_group"] or ""),
            payload=str(row["payload"] or ""),
            observed_at=float(row["observed_at"]),
            expires_at=float(row["expires_at"]),
            tombstoned_at=(
                float(row["tombstoned_at"])
                if row["tombstoned_at"] is not None
                else None
            ),
        )

    def lookup(
        self,
        scope: ResourceScope,
        name: str,
        *,
        fresh_only: bool = True,
        case_sensitive: bool = True,
        now: float | None = None,
    ) -> list[ResourceIdentity]:
        values = self._scope_values(scope)
        timestamp = float(time.time() if now is None else now)
        exact_name = str(name or "").strip()
        if not exact_name:
            return []
        sql = (
            """
            SELECT resource_id, name, owner_id, status, created_at,
                   compute_group, payload, observed_at, expires_at,
                   tombstoned_at
            FROM resource_identity
            WHERE base_url = ? AND subject_id = ? AND resource_type = ?
              AND workspace_id = ? AND owner_scope = ?
            """
        )
        params: list[object] = [*values]
        if case_sensitive:
            sql += " AND name = ?"
        else:
            sql += " AND name = ? COLLATE NOCASE"
        sql += " AND tombstoned_at IS NULL"
        params.append(exact_name)
        if fresh_only:
            sql += " AND expires_at > ?"
            params.append(timestamp)
        sql += " ORDER BY created_at DESC, observed_at DESC, resource_id"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row_identity(row) for row in rows]

    def lookup_id(
        self,
        scope: ResourceScope,
        resource_id: str,
        *,
        include_tombstoned: bool = False,
    ) -> ResourceIdentity | None:
        """Return one cached row by internal handle for invalidation/retry logic."""
        handle = str(resource_id or "").strip()
        if not handle:
            return None
        sql = (
            """
            SELECT resource_id, name, owner_id, status, created_at,
                   compute_group, payload, observed_at, expires_at,
                   tombstoned_at
            FROM resource_identity
            WHERE base_url = ? AND subject_id = ? AND resource_type = ?
              AND workspace_id = ? AND owner_scope = ?
              AND resource_id = ?
            """
        )
        if not include_tombstoned:
            sql += " AND tombstoned_at IS NULL"
        with self._connect() as connection:
            row = connection.execute(
                sql,
                (*self._scope_values(scope), handle),
            ).fetchone()
        return self._row_identity(row) if row is not None else None

    def list_identities(
        self,
        scope: ResourceScope,
        *,
        fresh_only: bool = True,
        now: float | None = None,
    ) -> list[ResourceIdentity]:
        """Return active identities in one scope without exposing them in CLI output."""
        timestamp = float(time.time() if now is None else now)
        sql = (
            """
            SELECT resource_id, name, owner_id, status, created_at,
                   compute_group, payload, observed_at, expires_at,
                   tombstoned_at
            FROM resource_identity
            WHERE base_url = ? AND subject_id = ? AND resource_type = ?
              AND workspace_id = ? AND owner_scope = ?
              AND tombstoned_at IS NULL
            """
        )
        params: list[object] = [*self._scope_values(scope)]
        if fresh_only:
            sql += " AND expires_at > ?"
            params.append(timestamp)
        sql += " ORDER BY name, resource_id"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row_identity(row) for row in rows]

    @staticmethod
    def _valid_records(records: Iterable[ResourceIdentity]) -> list[ResourceIdentity]:
        return [
            record
            for record in records
            if str(record.resource_id or "").strip() and str(record.name or "").strip()
        ]

    def _scope_revision_from_connection(
        self,
        connection: sqlite3.Connection,
        scope: ResourceScope,
    ) -> int:
        row = connection.execute(
            """
            SELECT mutation_revision
            FROM resource_scope
            WHERE base_url = ? AND subject_id = ? AND resource_type = ?
              AND workspace_id = ? AND owner_scope = ?
            """,
            self._scope_values(scope),
        ).fetchone()
        return int(row["mutation_revision"] or 0) if row is not None else 0

    @staticmethod
    def _generation_from_connection(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'cache_generation'"
        ).fetchone()
        return int(row["value"] or 0) if row is not None else 0

    def generation(self) -> int:
        """Return the account index generation used to invalidate in-flight work."""
        with self._connect() as connection:
            return self._generation_from_connection(connection)

    def scope_revision(self, scope: ResourceScope) -> int:
        """Return the current mutation revision for a cache scope."""
        scope = scope.validate()
        with self._connect() as connection:
            return self._scope_revision_from_connection(connection, scope)

    def snapshot_token(self, scope: ResourceScope) -> tuple[int, int]:
        """Capture generation and scope revision atomically before a live fetch."""
        scope = scope.validate()
        with self._connect() as connection:
            return (
                self._generation_from_connection(connection),
                self._scope_revision_from_connection(connection, scope),
            )

    def snapshot_workspace_refresh(
        self,
        workspace_scope: ResourceScope,
    ) -> tuple[int, int, dict[ResourceScope, int]]:
        """Capture the workspace scope and all child-scope revisions atomically."""
        workspace_scope = workspace_scope.validate()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT resource_type, workspace_id, owner_scope, mutation_revision
                FROM resource_scope
                WHERE base_url = ? AND subject_id = ? AND workspace_id <> ''
                """,
                (workspace_scope.base_url, workspace_scope.subject_id),
            ).fetchall()
            child_revisions = {
                ResourceScope(
                    base_url=workspace_scope.base_url,
                    subject_id=workspace_scope.subject_id,
                    resource_type=str(row["resource_type"]),
                    workspace_id=str(row["workspace_id"]),
                    owner_scope=str(row["owner_scope"] or ""),
                ): int(row["mutation_revision"] or 0)
                for row in rows
            }
            return (
                self._generation_from_connection(connection),
                self._scope_revision_from_connection(connection, workspace_scope),
                child_revisions,
            )

    def _begin_mutation(
        self,
        connection: sqlite3.Connection,
        scope: ResourceScope,
        *,
        expected_revision: int | None,
        expected_generation: int | None,
    ) -> int:
        """Serialize a cache mutation and reject stale refresh snapshots."""
        connection.execute("BEGIN IMMEDIATE")
        current_generation = self._generation_from_connection(connection)
        if (
            expected_generation is not None
            and current_generation != int(expected_generation)
        ):
            raise StaleResourceIndexRefresh(
                f"cache was cleared during refresh (expected generation "
                f"{expected_generation}, found {current_generation})"
            )
        current_revision = self._scope_revision_from_connection(connection, scope)
        if (
            expected_revision is not None
            and current_revision != int(expected_revision)
        ):
            raise StaleResourceIndexRefresh(
                f"cache scope changed during refresh (expected revision "
                f"{expected_revision}, found {current_revision})"
            )
        return current_revision

    def _bump_scope_revision(
        self,
        connection: sqlite3.Connection,
        scope: ResourceScope,
    ) -> int:
        """Increment the mutation revision after a committed identity change."""
        values = self._scope_values(scope)
        connection.execute(
            """
            INSERT INTO resource_scope(
                base_url, subject_id, resource_type, workspace_id, owner_scope
            )
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(
                base_url, subject_id, resource_type, workspace_id, owner_scope
            ) DO NOTHING
            """,
            values,
        )
        connection.execute(
            """
            UPDATE resource_scope
            SET mutation_revision = mutation_revision + 1
            WHERE base_url = ? AND subject_id = ? AND resource_type = ?
              AND workspace_id = ? AND owner_scope = ?
            """,
            self._scope_values(scope),
        )
        return self._scope_revision_from_connection(connection, scope)

    @staticmethod
    def _case_sensitive_for(
        resource_type: str,
        case_sensitive: bool | None,
    ) -> bool:
        if case_sensitive is not None:
            return case_sensitive
        return resource_type not in CASE_INSENSITIVE_RESOURCE_TYPES

    def _upsert_records(
        self,
        connection: sqlite3.Connection,
        scope: ResourceScope,
        records: Sequence[ResourceIdentity],
        *,
        ttl_seconds: int,
        observed_at: float,
        scan_id: str | None,
    ) -> None:
        values = self._scope_values(scope)
        expires_at = observed_at + max(0, int(ttl_seconds))
        for record in records:
            resource_id = str(record.resource_id or "").strip()
            name = str(record.name or "").strip()
            if not resource_id or not name:
                continue
            connection.execute(
                """
                INSERT INTO resource_identity(
                    base_url, subject_id, resource_type, workspace_id,
                    owner_scope, resource_id, name, owner_id, status,
                    created_at, compute_group, payload, observed_at,
                    expires_at, last_seen_scan_id, tombstoned_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(
                    base_url, subject_id, resource_type, workspace_id,
                    owner_scope, resource_id
                ) DO UPDATE SET
                    name=excluded.name,
                    owner_id=excluded.owner_id,
                    status=excluded.status,
                    created_at=excluded.created_at,
                    compute_group=excluded.compute_group,
                    payload=excluded.payload,
                    observed_at=excluded.observed_at,
                    expires_at=excluded.expires_at,
                    last_seen_scan_id=COALESCE(
                        excluded.last_seen_scan_id,
                        resource_identity.last_seen_scan_id
                    ),
                    tombstoned_at=NULL
                """,
                (
                    *values,
                    resource_id,
                    name,
                    str(record.owner_id or "").strip(),
                    str(record.status or "").strip(),
                    str(record.created_at or "").strip(),
                    str(record.compute_group or "").strip(),
                    str(record.payload or ""),
                    observed_at,
                    expires_at,
                    scan_id,
                ),
            )

    def upsert(
        self,
        scope: ResourceScope,
        records: Iterable[ResourceIdentity],
        *,
        ttl_seconds: int | None = None,
        now: float | None = None,
        expected_revision: int | None = None,
        expected_generation: int | None = None,
        attempted_at: float | None = None,
    ) -> int:
        """Insert observations without inferring that unseen records disappeared."""
        scope = scope.validate()
        items = self._valid_records(records)
        observed_at = float(time.time() if now is None else now)
        attempt_timestamp = float(
            observed_at if attempted_at is None else attempted_at
        )
        ttl = (
            DEFAULT_TTL_SECONDS.get(scope.resource_type, 60)
            if ttl_seconds is None
            else int(ttl_seconds)
        )
        with self._connect() as connection:
            self._begin_mutation(
                connection,
                scope,
                expected_revision=expected_revision,
                expected_generation=expected_generation,
            )
            self._upsert_records(
                connection,
                scope,
                items,
                ttl_seconds=ttl,
                observed_at=observed_at,
                scan_id=None,
            )
            self._write_scope_refresh(
                connection,
                scope,
                refreshed_at=observed_at,
                attempted_at=attempt_timestamp,
                full=False,
                scan_id=None,
                error="",
            )
            self._bump_scope_revision(connection, scope)
        return len(items)

    def replace_name(
        self,
        scope: ResourceScope,
        name: str,
        records: Iterable[ResourceIdentity],
        *,
        ttl_seconds: int | None = None,
        case_sensitive: bool | None = None,
        now: float | None = None,
        expected_revision: int | None = None,
        expected_generation: int | None = None,
        attempted_at: float | None = None,
    ) -> int:
        """Reconcile one exact name after a complete targeted lookup."""
        scope = scope.validate()
        exact_name = str(name or "").strip()
        if not exact_name:
            raise ValueError("resource name cannot be empty")
        match_case = self._case_sensitive_for(scope.resource_type, case_sensitive)
        items = [
            item
            for item in self._valid_records(records)
            if (
                str(item.name or "").strip() == exact_name
                if match_case
                else str(item.name or "").strip().casefold() == exact_name.casefold()
            )
        ]
        observed_at = float(time.time() if now is None else now)
        attempt_timestamp = float(
            observed_at if attempted_at is None else attempted_at
        )
        ttl = (
            DEFAULT_TTL_SECONDS.get(scope.resource_type, 60)
            if ttl_seconds is None
            else int(ttl_seconds)
        )
        ids = [str(item.resource_id).strip() for item in items]
        with self._connect() as connection:
            self._begin_mutation(
                connection,
                scope,
                expected_revision=expected_revision,
                expected_generation=expected_generation,
            )
            self._upsert_records(
                connection,
                scope,
                items,
                ttl_seconds=ttl,
                observed_at=observed_at,
                scan_id=None,
            )
            name_match = "" if match_case else "COLLATE NOCASE"
            sql = f"""
                UPDATE resource_identity
                SET tombstoned_at = ?, expires_at = ?
                WHERE base_url = ? AND subject_id = ? AND resource_type = ?
                  AND workspace_id = ? AND owner_scope = ?
                  AND name = ? {name_match} AND tombstoned_at IS NULL
                """
            params: list[object] = [observed_at, observed_at, *self._scope_values(scope), exact_name]
            if ids:
                sql += f" AND resource_id NOT IN ({','.join('?' for _ in ids)})"
                params.extend(ids)
            connection.execute(sql, params)
            self._write_scope_refresh(
                connection,
                scope,
                refreshed_at=observed_at,
                attempted_at=attempt_timestamp,
                full=False,
                scan_id=None,
                error="",
            )
            self._bump_scope_revision(connection, scope)
        return len(items)

    def reconcile(
        self,
        scope: ResourceScope,
        records: Iterable[ResourceIdentity],
        *,
        ttl_seconds: int | None = None,
        now: float | None = None,
        expected_revision: int | None = None,
        expected_generation: int | None = None,
        attempted_at: float | None = None,
    ) -> int:
        """Commit one complete scope scan and tombstone every unseen old row."""
        scope = scope.validate()
        items = self._valid_records(records)
        observed_at = float(time.time() if now is None else now)
        attempt_timestamp = float(
            observed_at if attempted_at is None else attempted_at
        )
        ttl = (
            DEFAULT_TTL_SECONDS.get(scope.resource_type, 60)
            if ttl_seconds is None
            else int(ttl_seconds)
        )
        scan_id = uuid.uuid4().hex
        with self._connect() as connection:
            self._begin_mutation(
                connection,
                scope,
                expected_revision=expected_revision,
                expected_generation=expected_generation,
            )
            self._upsert_records(
                connection,
                scope,
                items,
                ttl_seconds=ttl,
                observed_at=observed_at,
                scan_id=scan_id,
            )
            connection.execute(
                """
                UPDATE resource_identity
                SET tombstoned_at = ?, expires_at = ?
                WHERE base_url = ? AND subject_id = ? AND resource_type = ?
                  AND workspace_id = ? AND owner_scope = ?
                  AND tombstoned_at IS NULL
                  AND COALESCE(last_seen_scan_id, '') != ?
                """,
                (observed_at, observed_at, *self._scope_values(scope), scan_id),
            )
            self._write_scope_refresh(
                connection,
                scope,
                refreshed_at=observed_at,
                attempted_at=attempt_timestamp,
                full=True,
                scan_id=scan_id,
                error="",
            )
            self._bump_scope_revision(connection, scope)
        return len(items)

    def _write_scope_refresh(
        self,
        connection: sqlite3.Connection,
        scope: ResourceScope,
        *,
        refreshed_at: float,
        attempted_at: float,
        full: bool,
        scan_id: str | None,
        error: str,
    ) -> None:
        values = self._scope_values(scope)
        connection.execute(
            """
            INSERT INTO resource_scope(
                base_url, subject_id, resource_type, workspace_id, owner_scope,
                last_attempt_at, last_refresh_at, last_full_refresh_at, last_scan_id,
                refresh_complete, last_error, mutation_revision
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(
                base_url, subject_id, resource_type, workspace_id, owner_scope
            ) DO UPDATE SET
                last_attempt_at=MAX(
                    resource_scope.last_attempt_at,
                    excluded.last_attempt_at
                ),
                last_refresh_at=MAX(
                    resource_scope.last_refresh_at,
                    excluded.last_refresh_at
                ),
                last_full_refresh_at=CASE
                    WHEN excluded.refresh_complete = 1
                     AND excluded.last_attempt_at >= resource_scope.last_attempt_at
                     AND excluded.last_full_refresh_at >= resource_scope.last_full_refresh_at
                    THEN excluded.last_full_refresh_at
                    ELSE resource_scope.last_full_refresh_at
                END,
                last_scan_id=CASE
                    WHEN excluded.refresh_complete = 1
                     AND excluded.last_attempt_at >= resource_scope.last_attempt_at
                     AND excluded.last_full_refresh_at >= resource_scope.last_full_refresh_at
                    THEN excluded.last_scan_id
                    ELSE resource_scope.last_scan_id
                END,
                refresh_complete=CASE
                    WHEN excluded.refresh_complete = 1
                     AND excluded.last_attempt_at >= resource_scope.last_attempt_at
                     AND excluded.last_full_refresh_at >= resource_scope.last_full_refresh_at
                    THEN 1
                    ELSE resource_scope.refresh_complete
                END,
                last_error=CASE
                    WHEN excluded.last_attempt_at < resource_scope.last_attempt_at
                    THEN resource_scope.last_error
                    WHEN excluded.refresh_complete = 1
                     AND excluded.last_attempt_at >= resource_scope.last_attempt_at
                     AND excluded.last_full_refresh_at >= resource_scope.last_full_refresh_at
                    THEN excluded.last_error
                    ELSE resource_scope.last_error
                END
            """,
            (
                *values,
                attempted_at,
                refreshed_at,
                refreshed_at if full else 0,
                scan_id,
                1 if full else 0,
                str(error or ""),
            ),
        )

    def record_refresh_error(
        self,
        scope: ResourceScope,
        error: str,
        *,
        now: float | None = None,
    ) -> None:
        timestamp = float(time.time() if now is None else now)
        scope = scope.validate()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO resource_scope(
                        base_url, subject_id, resource_type, workspace_id, owner_scope,
                        last_attempt_at, last_refresh_at, last_full_refresh_at,
                        last_scan_id, refresh_complete, last_error, mutation_revision
                    )
                    VALUES(?, ?, ?, ?, ?, ?, 0, 0, NULL, 0, ?, 0)
                    ON CONFLICT(
                        base_url, subject_id, resource_type, workspace_id, owner_scope
                    ) DO UPDATE SET
                        last_attempt_at=MAX(
                            resource_scope.last_attempt_at,
                            excluded.last_attempt_at
                        ),
                        last_error=CASE
                            WHEN excluded.last_attempt_at >= resource_scope.last_attempt_at
                            THEN excluded.last_error
                            ELSE resource_scope.last_error
                        END
                    """,
                    (*self._scope_values(scope), timestamp, str(error or "")),
                )
        except (OSError, sqlite3.Error):
            # Refresh diagnostics are best effort and must not replace a
            # usable snapshot with a database error.
            pass

    def mark_deleted(
        self,
        scope: ResourceScope,
        *,
        resource_id: str = "",
        name: str = "",
        allow_name_fallback: bool = True,
        now: float | None = None,
    ) -> int:
        """Tombstone by ID, falling back to name when the ID is unknown.

        The ID is authoritative when it is present in the cache. A name is
        only used as a fallback for an ID that was never indexed, avoiding a
        fragile ``id AND name`` match without deleting a newly-recreated
        same-name resource when a previous handle is already known. Callers handling
        a stale platform handle can disable that fallback because a clear
        may have removed the previous handle before a same-name replacement was cached.
        """
        target_id = str(resource_id or "").strip()
        target_name = str(name or "").strip()
        if not target_id and not target_name:
            return 0
        timestamp = float(time.time() if now is None else now)
        scope = scope.validate()
        values = self._scope_values(scope)
        match_case = self._case_sensitive_for(scope.resource_type, None)
        with self._connect() as connection:
            self._begin_mutation(
                connection,
                scope,
                expected_revision=None,
                expected_generation=None,
            )
            changed = 0
            known_id = False
            if target_id:
                known_id_row = connection.execute(
                    """
                    SELECT 1
                    FROM resource_identity
                    WHERE base_url = ? AND subject_id = ? AND resource_type = ?
                      AND workspace_id = ? AND owner_scope = ? AND resource_id = ?
                    LIMIT 1
                    """,
                    (*values, target_id),
                ).fetchone()
                known_id = known_id_row is not None
                if known_id:
                    cursor = connection.execute(
                        """
                        UPDATE resource_identity
                        SET tombstoned_at = ?, expires_at = ?
                        WHERE base_url = ? AND subject_id = ? AND resource_type = ?
                          AND workspace_id = ? AND owner_scope = ?
                          AND resource_id = ? AND tombstoned_at IS NULL
                        """,
                        (timestamp, timestamp, *values, target_id),
                    )
                    changed = int(cursor.rowcount)

            if (
                allow_name_fallback
                and changed == 0
                and target_name
                and (not target_id or not known_id)
            ):
                name_match = "" if match_case else "COLLATE NOCASE"
                cursor = connection.execute(
                    f"""
                    UPDATE resource_identity
                    SET tombstoned_at = ?, expires_at = ?
                    WHERE base_url = ? AND subject_id = ? AND resource_type = ?
                      AND workspace_id = ? AND owner_scope = ?
                      AND name = ? {name_match} AND tombstoned_at IS NULL
                    """,
                    (timestamp, timestamp, *values, target_name),
                )
                changed = int(cursor.rowcount)

            # Even when the row was not indexed yet, a delete response is a
            # mutation boundary.  Bumping the revision prevents an older
            # in-flight refresh from resurrecting the deleted handle.
            self._bump_scope_revision(connection, scope)
            return changed

    def scope_due(
        self,
        scope: ResourceScope,
        *,
        interval_seconds: int,
        now: float | None = None,
        require_full: bool = False,
    ) -> bool:
        timestamp = float(time.time() if now is None else now)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT last_refresh_at, last_full_refresh_at
                FROM resource_scope
                WHERE base_url = ? AND subject_id = ? AND resource_type = ?
                  AND workspace_id = ? AND owner_scope = ?
                """,
                self._scope_values(scope),
            ).fetchone()
        if row is None:
            return True
        last = float(row["last_full_refresh_at"] if require_full else row["last_refresh_at"])
        return last <= 0 or timestamp - last >= max(0, int(interval_seconds))

    def list_scope_status(self, *, now: float | None = None) -> list[ScopeStatus]:
        timestamp = float(time.time() if now is None else now)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    s.resource_type,
                    s.workspace_id,
                    s.last_refresh_at,
                    s.last_full_refresh_at,
                    s.last_error,
                    SUM(
                        CASE
                            WHEN r.resource_id IS NOT NULL
                             AND r.tombstoned_at IS NULL
                             AND r.expires_at > ?
                            THEN 1 ELSE 0
                        END
                    ) active_count
                FROM resource_scope s
                LEFT JOIN resource_identity r
                  ON r.base_url=s.base_url
                 AND r.subject_id=s.subject_id
                 AND r.resource_type=s.resource_type
                 AND r.workspace_id=s.workspace_id
                 AND r.owner_scope=s.owner_scope
                GROUP BY
                    s.base_url, s.subject_id, s.resource_type,
                    s.workspace_id, s.owner_scope
                ORDER BY s.resource_type, s.workspace_id, s.owner_scope
                """,
                (timestamp,),
            ).fetchall()
        return [
            ScopeStatus(
                resource_type=str(row["resource_type"]),
                workspace_id=str(row["workspace_id"] or ""),
                active_count=int(row["active_count"] or 0),
                last_refresh_at=float(row["last_refresh_at"] or 0),
                last_full_refresh_at=float(row["last_full_refresh_at"] or 0),
                last_error=str(row["last_error"] or ""),
            )
            for row in rows
        ]

    def prune_orphan_workspace_scopes(
        self,
        workspace_scope: ResourceScope,
        visible_workspace_ids: Iterable[str],
        *,
        expected_generation: int,
        expected_workspace_revision: int,
        expected_child_revisions: Mapping[ResourceScope, int],
    ) -> int:
        """Remove workspace-bound scopes no longer visible in a complete workspace scan."""
        workspace_scope = workspace_scope.validate()
        visible = {
            str(workspace_id or "").strip()
            for workspace_id in visible_workspace_ids
            if str(workspace_id or "").strip()
        }
        with self._connect() as connection:
            self._begin_mutation(
                connection,
                workspace_scope,
                expected_revision=expected_workspace_revision,
                expected_generation=expected_generation,
            )
            rows = connection.execute(
                """
                SELECT resource_type, workspace_id, owner_scope, mutation_revision
                FROM resource_scope
                WHERE base_url = ? AND subject_id = ? AND workspace_id <> ''
                """,
                (workspace_scope.base_url, workspace_scope.subject_id),
            ).fetchall()
            orphan_scopes = [
                ResourceScope(
                    base_url=workspace_scope.base_url,
                    subject_id=workspace_scope.subject_id,
                    resource_type=str(row["resource_type"]),
                    workspace_id=str(row["workspace_id"]),
                    owner_scope=str(row["owner_scope"] or ""),
                )
                for row in rows
                if str(row["workspace_id"]) not in visible
                and expected_child_revisions.get(
                    ResourceScope(
                        base_url=workspace_scope.base_url,
                        subject_id=workspace_scope.subject_id,
                        resource_type=str(row["resource_type"]),
                        workspace_id=str(row["workspace_id"]),
                        owner_scope=str(row["owner_scope"] or ""),
                    )
                )
                == int(row["mutation_revision"] or 0)
            ]
            for scope in orphan_scopes:
                values = self._scope_values(scope)
                connection.execute(
                    """
                    DELETE FROM resource_identity
                    WHERE base_url = ? AND subject_id = ? AND resource_type = ?
                      AND workspace_id = ? AND owner_scope = ?
                    """,
                    values,
                )
                connection.execute(
                    """
                    DELETE FROM resource_scope
                    WHERE base_url = ? AND subject_id = ? AND resource_type = ?
                      AND workspace_id = ? AND owner_scope = ?
                    """,
                    values,
                )
                connection.execute(
                    "DELETE FROM refresh_lease WHERE lease_key = ?",
                    (scope.lease_key(),),
                )
            if orphan_scopes:
                generation = self._generation_from_connection(connection) + 1
                connection.execute(
                    """
                    INSERT INTO metadata(key, value) VALUES('cache_generation', ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (str(generation),),
                )
            return len(orphan_scopes)

    def purge_tombstones(
        self,
        *,
        older_than_seconds: int = 7 * 24 * 60 * 60,
        now: float | None = None,
    ) -> int:
        threshold = float(time.time() if now is None else now) - max(
            0, int(older_than_seconds)
        )
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    DELETE FROM resource_identity
                    WHERE tombstoned_at IS NOT NULL AND tombstoned_at < ?
                    """,
                    (threshold,),
                )
                return int(cursor.rowcount)
        except (OSError, sqlite3.Error):
            # Tombstone cleanup is disposable maintenance.  A cache failure
            # must not affect the last successful identity snapshot.
            return 0

    @contextlib.contextmanager
    def refresh_lease(
        self,
        scope: ResourceScope,
        *,
        lease_seconds: int = 120,
        holder: str | None = None,
        now: float | None = None,
        raise_on_error: bool = False,
    ) -> Iterator[bool]:
        """Acquire a per-scope single-flight lease and renew it while held."""
        timestamp = float(time.time() if now is None else now)
        lease_key = scope.lease_key()
        owner = holder or f"{os.getpid()}:{uuid.uuid4().hex}"
        acquired = False
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM refresh_lease WHERE lease_key = ? AND expires_at <= ?",
                    (lease_key, timestamp),
                )
                try:
                    connection.execute(
                        """
                        INSERT INTO refresh_lease(lease_key, holder, expires_at)
                        VALUES(?, ?, ?)
                        """,
                        (lease_key, owner, timestamp + max(1, int(lease_seconds))),
                    )
                    acquired = True
                except sqlite3.IntegrityError:
                    acquired = False
        except (OSError, sqlite3.Error) as exc:
            if raise_on_error:
                raise ResourceIndexDatabaseError(
                    "The local resource name cache is unavailable."
                ) from exc
            acquired = False
        stop_heartbeat = threading.Event()
        heartbeat: threading.Thread | None = None
        if acquired:
            heartbeat_interval = max(0.5, min(float(lease_seconds) / 3.0, 30.0))

            def _heartbeat() -> None:
                while not stop_heartbeat.wait(heartbeat_interval):
                    try:
                        with self._connect() as connection:
                            connection.execute(
                                """
                                UPDATE refresh_lease
                                SET expires_at = ?
                                WHERE lease_key = ? AND holder = ?
                                """,
                                (
                                    time.time() + max(1, int(lease_seconds)),
                                    lease_key,
                                    owner,
                                ),
                            )
                    except (OSError, sqlite3.Error):
                        # If the database is unavailable, allowing this lease
                        # to expire is safer than blocking future refreshes.
                        return

            heartbeat = threading.Thread(
                target=_heartbeat,
                name="inspire-resource-index-lease",
                daemon=True,
            )
            heartbeat.start()
        try:
            yield acquired
        finally:
            if acquired:
                stop_heartbeat.set()
                if heartbeat is not None:
                    heartbeat.join(timeout=1.0)
            if acquired:
                try:
                    with self._connect() as connection:
                        connection.execute(
                            "DELETE FROM refresh_lease WHERE lease_key = ? AND holder = ?",
                            (lease_key, owner),
                        )
                except (OSError, sqlite3.Error):
                    # A failed release is safe: the lease has a finite TTL,
                    # and cleanup must never mask the command result.
                    pass

    def clear(self, resource_types: Iterable[str] | None = None) -> int:
        """Delete cached identities and refresh metadata; return names removed.

        ``resource_types`` narrows the delete to those kinds. A partial clear
        still bumps the generation: an in-flight refresh holding a snapshot of
        any scope must not write its results over an emptied one.
        """
        selected = [
            normalized
            for resource_type in (resource_types or ())
            if (normalized := str(resource_type or "").strip().lower())
        ]
        if resource_types is not None and not selected:
            return 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            generation = self._generation_from_connection(connection) + 1
            if selected:
                placeholders = ",".join("?" for _ in selected)
                where = f" WHERE resource_type IN ({placeholders})"
                removed = connection.execute(
                    f"DELETE FROM resource_identity{where}", selected
                ).rowcount
                connection.execute(f"DELETE FROM resource_scope{where}", selected)
            else:
                removed = connection.execute("DELETE FROM resource_identity").rowcount
                connection.execute("DELETE FROM resource_scope")
            # Leases are keyed by an opaque scope string, so a narrowed clear
            # cannot pick out its own. Dropping all of them only forces the
            # affected refreshes to re-acquire.
            connection.execute("DELETE FROM refresh_lease")
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES('cache_generation', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(generation),),
            )
        return max(0, int(removed))


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "QUOTA_RESOURCE_TYPES",
    "QUOTA_WORKLOADS",
    "RESOURCE_INDEX_FILENAME",
    "GLOBAL_RESOURCE_TYPES",
    "ResourceIdentity",
    "ResourceIndex",
    "ResourceIndexDatabaseError",
    "ResourceScope",
    "ScopeStatus",
    "StaleResourceIndexRefresh",
    "quota_resource_type",
    "resource_index_path",
    "scope_for_session",
    "scope_workspace_id",
]
