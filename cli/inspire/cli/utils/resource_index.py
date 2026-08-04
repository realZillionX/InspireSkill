"""Disposable per-account resource identity index.

The platform remains the sole source of truth.  This SQLite database only
accelerates name-to-handle resolution; it must never become the backing store
for normal ``list`` or status output.
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

SCHEMA_VERSION = 1
RESOURCE_INDEX_FILENAME = "resource-index.sqlite3"

DEFAULT_TTL_SECONDS: dict[str, int] = {
    "workspace": 5 * 60,
    "project": 5 * 60,
    "compute-group": 5 * 60,
    "image": 5 * 60,
    "model": 5 * 60,
    "job": 60,
    "hpc": 60,
    "ray": 60,
    "serving": 60,
    "notebook": 60,
    "ssh-key": 5 * 60,
}

CASE_INSENSITIVE_RESOURCE_TYPES = frozenset(
    {"workspace", "project", "compute-group"}
)


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
    owner_scope: str
    active_count: int
    tombstone_count: int
    last_attempt_at: float
    last_refresh_at: float
    last_full_refresh_at: float
    refresh_complete: bool
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
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

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
                   observed_at, expires_at, tombstoned_at
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
                   observed_at, expires_at, tombstoned_at
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

    @staticmethod
    def _valid_records(records: Iterable[ResourceIdentity]) -> list[ResourceIdentity]:
        return [
            record
            for record in records
            if str(record.resource_id or "").strip() and str(record.name or "").strip()
        ]

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
                    created_at, observed_at, expires_at, last_seen_scan_id,
                    tombstoned_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(
                    base_url, subject_id, resource_type, workspace_id,
                    owner_scope, resource_id
                ) DO UPDATE SET
                    name=excluded.name,
                    owner_id=excluded.owner_id,
                    status=excluded.status,
                    created_at=excluded.created_at,
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
    ) -> int:
        """Insert observations without inferring that unseen records disappeared."""
        scope = scope.validate()
        items = self._valid_records(records)
        observed_at = float(time.time() if now is None else now)
        ttl = (
            DEFAULT_TTL_SECONDS.get(scope.resource_type, 60)
            if ttl_seconds is None
            else int(ttl_seconds)
        )
        with self._connect() as connection:
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
                full=False,
                scan_id=None,
                error="",
            )
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
        ttl = (
            DEFAULT_TTL_SECONDS.get(scope.resource_type, 60)
            if ttl_seconds is None
            else int(ttl_seconds)
        )
        ids = [str(item.resource_id).strip() for item in items]
        with self._connect() as connection:
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
                full=False,
                scan_id=None,
                error="",
            )
        return len(items)

    def reconcile(
        self,
        scope: ResourceScope,
        records: Iterable[ResourceIdentity],
        *,
        ttl_seconds: int | None = None,
        now: float | None = None,
    ) -> int:
        """Commit one complete scope scan and tombstone every unseen old row."""
        scope = scope.validate()
        items = self._valid_records(records)
        observed_at = float(time.time() if now is None else now)
        ttl = (
            DEFAULT_TTL_SECONDS.get(scope.resource_type, 60)
            if ttl_seconds is None
            else int(ttl_seconds)
        )
        scan_id = uuid.uuid4().hex
        with self._connect() as connection:
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
                full=True,
                scan_id=scan_id,
                error="",
            )
        return len(items)

    def _write_scope_refresh(
        self,
        connection: sqlite3.Connection,
        scope: ResourceScope,
        *,
        refreshed_at: float,
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
                refresh_complete, last_error
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                base_url, subject_id, resource_type, workspace_id, owner_scope
            ) DO UPDATE SET
                last_attempt_at=excluded.last_attempt_at,
                last_refresh_at=excluded.last_refresh_at,
                last_full_refresh_at=CASE
                    WHEN excluded.refresh_complete = 1
                    THEN excluded.last_full_refresh_at
                    ELSE resource_scope.last_full_refresh_at
                END,
                last_scan_id=CASE
                    WHEN excluded.refresh_complete = 1
                    THEN excluded.last_scan_id
                    ELSE resource_scope.last_scan_id
                END,
                refresh_complete=CASE
                    WHEN excluded.refresh_complete = 1 THEN 1
                    ELSE resource_scope.refresh_complete
                END,
                last_error=excluded.last_error
            """,
            (
                *values,
                refreshed_at,
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
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO resource_scope(
                    base_url, subject_id, resource_type, workspace_id, owner_scope,
                    last_attempt_at, last_refresh_at, last_full_refresh_at,
                    last_scan_id, refresh_complete, last_error
                )
                VALUES(?, ?, ?, ?, ?, ?, 0, 0, NULL, 0, ?)
                ON CONFLICT(
                    base_url, subject_id, resource_type, workspace_id, owner_scope
                ) DO UPDATE SET
                    last_attempt_at=excluded.last_attempt_at,
                    last_error=excluded.last_error
                """,
                (*self._scope_values(scope), timestamp, str(error or "")),
            )

    def mark_deleted(
        self,
        scope: ResourceScope,
        *,
        resource_id: str = "",
        name: str = "",
        now: float | None = None,
    ) -> int:
        """Tombstone by ID, falling back to name when the ID is unknown.

        The ID is authoritative when it is present in the cache. A name is
        only used as a fallback for an ID that was never indexed, avoiding a
        fragile ``id AND name`` match without deleting a newly-recreated
        same-name resource when an old ID is already known.
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
            if target_id:
                known_id = connection.execute(
                    """
                    SELECT 1
                    FROM resource_identity
                    WHERE base_url = ? AND subject_id = ? AND resource_type = ?
                      AND workspace_id = ? AND owner_scope = ? AND resource_id = ?
                    LIMIT 1
                    """,
                    (*values, target_id),
                ).fetchone()
                if known_id is not None:
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
                    return int(cursor.rowcount)

            if not target_name:
                return 0
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
            return int(cursor.rowcount)

    def mark_scope_stale(self, scope: ResourceScope) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE resource_identity
                SET expires_at = 0
                WHERE base_url = ? AND subject_id = ? AND resource_type = ?
                  AND workspace_id = ? AND owner_scope = ?
                  AND tombstoned_at IS NULL
                """,
                self._scope_values(scope),
            )
            return int(cursor.rowcount)

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

    def list_scope_status(self) -> list[ScopeStatus]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    s.base_url,
                    s.subject_id,
                    s.resource_type,
                    s.workspace_id,
                    s.owner_scope,
                    s.last_attempt_at,
                    s.last_refresh_at,
                    s.last_full_refresh_at,
                    s.refresh_complete,
                    s.last_error,
                    SUM(
                        CASE
                            WHEN r.resource_id IS NOT NULL
                             AND r.tombstoned_at IS NULL
                            THEN 1 ELSE 0
                        END
                    ) active_count,
                    SUM(CASE WHEN r.tombstoned_at IS NOT NULL THEN 1 ELSE 0 END) tombstone_count
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
                """
            ).fetchall()
        return [
            ScopeStatus(
                resource_type=str(row["resource_type"]),
                workspace_id=str(row["workspace_id"] or ""),
                owner_scope=str(row["owner_scope"] or ""),
                active_count=int(row["active_count"] or 0),
                tombstone_count=int(row["tombstone_count"] or 0),
                last_attempt_at=float(row["last_attempt_at"] or 0),
                last_refresh_at=float(row["last_refresh_at"] or 0),
                last_full_refresh_at=float(row["last_full_refresh_at"] or 0),
                refresh_complete=bool(row["refresh_complete"]),
                last_error=str(row["last_error"] or ""),
            )
            for row in rows
        ]

    def purge_tombstones(
        self,
        *,
        older_than_seconds: int = 7 * 24 * 60 * 60,
        now: float | None = None,
    ) -> int:
        threshold = float(time.time() if now is None else now) - max(
            0, int(older_than_seconds)
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM resource_identity
                WHERE tombstoned_at IS NOT NULL AND tombstoned_at < ?
                """,
                (threshold,),
            )
            return int(cursor.rowcount)

    @contextlib.contextmanager
    def refresh_lease(
        self,
        scope: ResourceScope,
        *,
        lease_seconds: int = 120,
        holder: str | None = None,
        now: float | None = None,
    ) -> Iterator[bool]:
        """Acquire a per-scope single-flight lease and renew it while held."""
        timestamp = float(time.time() if now is None else now)
        lease_key = scope.lease_key()
        owner = holder or f"{os.getpid()}:{uuid.uuid4().hex}"
        acquired = False
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
                with self._connect() as connection:
                    connection.execute(
                        "DELETE FROM refresh_lease WHERE lease_key = ? AND holder = ?",
                        (lease_key, owner),
                    )

    def clear(self) -> None:
        """Delete all cached identity and refresh metadata."""
        with self._connect() as connection:
            connection.execute("DELETE FROM resource_identity")
            connection.execute("DELETE FROM resource_scope")
            connection.execute("DELETE FROM refresh_lease")


def candidates_from_dicts(
    candidates: Iterable[Mapping[str, object]],
    *,
    name_key: str = "name",
    id_key: str = "id",
) -> list[ResourceIdentity]:
    """Convert resolver candidate dictionaries into minimal cache rows."""
    records: list[ResourceIdentity] = []
    for candidate in candidates:
        resource_id = str(candidate.get(id_key) or "").strip()
        name = str(candidate.get(name_key) or "").strip()
        if not resource_id or not name:
            continue
        records.append(
            ResourceIdentity(
                resource_id=resource_id,
                name=name,
                owner_id=str(
                    candidate.get("owner_id")
                    or candidate.get("created_by_id")
                    or candidate.get("user_id")
                    or ""
                ).strip(),
                status=str(candidate.get("status") or "").strip(),
                created_at=str(candidate.get("created_at") or "").strip(),
            )
        )
    return records


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "RESOURCE_INDEX_FILENAME",
    "ResourceIdentity",
    "ResourceIndex",
    "ResourceScope",
    "ScopeStatus",
    "candidates_from_dicts",
    "resource_index_path",
    "scope_for_session",
]
