from __future__ import annotations

import os
import sqlite3
import time

import pytest

from inspire.accounts import create_account, set_current_account
from inspire.cli.utils.resource_index import (
    ResourceIdentity,
    ResourceIndex,
    ResourceIndexDatabaseError,
    ResourceScope,
    StaleResourceIndexRefresh,
    resource_index_path,
    scope_for_session,
)


def _scope(
    *,
    resource_type: str = "job",
    workspace_id: str = "workspace-one",
    subject_id: str = "user-one",
) -> ResourceScope:
    return ResourceScope(
        base_url="https://inspire.example",
        subject_id=subject_id,
        resource_type=resource_type,
        workspace_id=workspace_id,
        owner_scope="self",
    )


def _record(
    resource_id: str,
    name: str,
    *,
    status: str = "",
    created_at: str = "",
) -> ResourceIdentity:
    return ResourceIdentity(
        resource_id=resource_id,
        name=name,
        status=status,
        created_at=created_at,
    )


def _scope_metadata(index: ResourceIndex, scope: ResourceScope) -> sqlite3.Row:
    with index._connect() as connection:
        row = connection.execute(
            """
            SELECT last_attempt_at, refresh_complete
            FROM resource_scope
            WHERE base_url = ? AND subject_id = ? AND resource_type = ?
              AND workspace_id = ? AND owner_scope = ?
            """,
            (
                scope.base_url,
                scope.subject_id,
                scope.resource_type,
                scope.workspace_id,
                scope.owner_scope,
            ),
        ).fetchone()
    assert row is not None
    return row


def test_lookup_respects_freshness_and_scope(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()
    other_scope = _scope(workspace_id="workspace-two")

    index.upsert(scope, [_record("job-one", "train")], ttl_seconds=10, now=100)
    index.upsert(other_scope, [_record("job-two", "train")], ttl_seconds=10, now=100)

    assert [item.resource_id for item in index.lookup(scope, "train", now=105)] == [
        "job-one"
    ]
    assert index.lookup(scope, "train", now=111) == []
    assert [
        item.resource_id
        for item in index.lookup(scope, "train", fresh_only=False, now=111)
    ] == ["job-one"]


def test_lookup_can_match_workspace_names_case_insensitively(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope(resource_type="workspace", workspace_id="")
    index.upsert(scope, [_record("workspace-one", "Training Space")], now=100)

    assert index.lookup(scope, "training space", now=101) == []
    assert [
        item.resource_id
        for item in index.lookup(
            scope,
            "training space",
            case_sensitive=False,
            now=101,
        )
    ] == ["workspace-one"]


def test_replace_name_tombstones_deleted_and_recreated_identity(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope(resource_type="notebook")
    index.replace_name(scope, "A", [_record("notebook-old", "A")], now=100)

    index.replace_name(scope, "A", [_record("notebook-new", "A")], now=200)

    assert [
        item.resource_id
        for item in index.lookup(scope, "A", fresh_only=False, now=200)
    ] == ["notebook-new"]
    old = index.lookup_id(scope, "notebook-old", include_tombstoned=True)
    assert old is not None
    assert old.tombstoned_at == 200


def test_replace_name_with_no_results_tombstones_only_that_name(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()
    index.upsert(
        scope,
        [_record("job-a", "A"), _record("job-b", "B")],
        now=100,
    )

    index.replace_name(scope, "A", [], now=200)

    assert index.lookup(scope, "A", fresh_only=False, now=200) == []
    assert [
        item.resource_id
        for item in index.lookup(scope, "B", fresh_only=False, now=200)
    ] == ["job-b"]


def test_mark_deleted_with_stale_name_uses_known_id(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()
    index.upsert(scope, [_record("job-a", "renamed")], now=100)

    assert (
        index.mark_deleted(
            scope,
            resource_id="job-a",
            name="old-name",
            now=200,
        )
        == 1
    )
    deleted = index.lookup_id(scope, "job-a", include_tombstoned=True)
    assert deleted is not None
    assert deleted.tombstoned_at == 200


def test_mark_deleted_does_not_fallback_to_same_name_for_known_tombstoned_id(
    tmp_path,
) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()
    index.upsert(
        scope,
        [_record("job-old", "A"), _record("job-new", "A")],
        now=100,
    )
    index.mark_deleted(scope, resource_id="job-old", now=150)

    assert (
        index.mark_deleted(
            scope,
            resource_id="job-old",
            name="A",
            now=200,
        )
        == 0
    )
    replacement = index.lookup_id(scope, "job-new")
    assert replacement is not None


def test_mark_deleted_exact_id_does_not_tombstone_same_name_replacement(
    tmp_path,
) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()
    index.upsert(scope, [_record("job-old", "A")])

    index.clear()
    index.upsert(scope, [_record("job-new", "A")])

    assert (
        index.mark_deleted(
            scope,
            resource_id="job-old",
            name="A",
            allow_name_fallback=False,
        )
        == 0
    )
    replacement = index.lookup_id(scope, "job-new")
    assert replacement is not None
    assert replacement.tombstoned_at is None


@pytest.mark.parametrize("resource_type", ["workspace", "project", "compute-group"])
def test_replace_name_is_case_insensitive_for_case_insensitive_resources(
    tmp_path,
    resource_type: str,
) -> None:
    index = ResourceIndex(tmp_path / f"{resource_type}.sqlite3")
    scope = _scope(resource_type=resource_type, workspace_id="")
    index.upsert(scope, [_record("old-id", "Training Space")], now=100)

    index.replace_name(
        scope,
        "training space",
        [_record("new-id", "TRAINING SPACE")],
        now=200,
    )

    active = index.lookup(
        scope,
        "Training Space",
        fresh_only=False,
        now=200,
        case_sensitive=False,
    )
    assert [item.resource_id for item in active] == ["new-id"]
    old = index.lookup_id(scope, "old-id", include_tombstoned=True)
    assert old is not None
    assert old.tombstoned_at == 200


def test_partial_upsert_never_tombstones_unseen_rows(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()
    index.reconcile(
        scope,
        [_record("job-a", "A"), _record("job-b", "B")],
        now=100,
    )

    index.upsert(scope, [_record("job-a", "A", status="RUNNING")], now=200)

    assert [
        item.resource_id
        for item in index.lookup(scope, "B", fresh_only=False, now=200)
    ] == ["job-b"]


def test_full_reconcile_tombstones_unseen_rows(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()
    index.reconcile(
        scope,
        [_record("job-a", "A"), _record("job-b", "B")],
        now=100,
    )

    index.reconcile(scope, [_record("job-a", "A")], now=200)

    assert index.lookup(scope, "B", fresh_only=False, now=200) == []
    deleted = index.lookup_id(scope, "job-b", include_tombstoned=True)
    assert deleted is not None
    assert deleted.tombstoned_at == 200


def test_old_full_refresh_cannot_resurrect_deleted_and_recreated_name(
    tmp_path,
) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope(resource_type="notebook")
    index.upsert(scope, [_record("notebook-old", "A")], now=100)
    refresh_revision = index.scope_revision(scope)

    # A user action wins while the platform list used by the old refresh is
    # still in flight.
    index.mark_deleted(scope, resource_id="notebook-old", now=110)
    index.upsert(scope, [_record("notebook-new", "A")], now=111)

    with pytest.raises(StaleResourceIndexRefresh):
        index.reconcile(
            scope,
            [_record("notebook-old", "A")],
            now=112,
            expected_revision=refresh_revision,
        )

    assert [
        item.resource_id
        for item in index.lookup(scope, "A", fresh_only=False, now=112)
    ] == ["notebook-new"]
    old = index.lookup_id(scope, "notebook-old", include_tombstoned=True)
    assert old is not None
    assert old.tombstoned_at == 110


def test_scope_refresh_timestamps_never_move_backwards(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()
    index.reconcile(scope, [_record("job-a", "A")], now=200)
    index.replace_name(scope, "A", [_record("job-a", "A")], now=100)

    status = index.list_scope_status()[0]
    assert status.last_refresh_at == 200
    assert status.last_full_refresh_at == 200


def test_refresh_error_preserves_last_successful_snapshot(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()
    index.reconcile(scope, [_record("job-a", "A")], now=100)

    index.record_refresh_error(scope, "network unavailable", now=200)

    status = index.list_scope_status()[0]
    assert status.last_refresh_at == 100
    assert status.last_full_refresh_at == 100
    assert status.last_error == "network unavailable"
    metadata = _scope_metadata(index, scope)
    assert metadata["last_attempt_at"] == 200
    assert metadata["refresh_complete"] == 1
    assert [
        item.resource_id
        for item in index.lookup(scope, "A", fresh_only=False, now=200)
    ] == ["job-a"]


def test_older_refresh_error_cannot_overwrite_newer_success(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()
    index.reconcile(
        scope,
        [_record("job-a", "A")],
        now=200,
        attempted_at=200,
    )

    index.record_refresh_error(scope, "old failure", now=100)

    status = index.list_scope_status()[0]
    assert status.last_error == ""
    assert _scope_metadata(index, scope)["last_attempt_at"] == 200


def test_write_through_preserves_full_refresh_error_until_full_success(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()
    index.reconcile(scope, [_record("job-a", "A")], now=100)
    index.record_refresh_error(scope, "full refresh failed", now=200)

    index.upsert(scope, [_record("job-b", "B")], now=201)

    status = index.list_scope_status()[0]
    assert status.last_error == "full refresh failed"
    index.reconcile(scope, [_record("job-b", "B")], now=202, attempted_at=202)
    assert index.list_scope_status()[0].last_error == ""


def test_scope_status_reports_active_rows_without_losing_expired_or_tombstoned_rows(
    tmp_path,
) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()
    index.reconcile(
        scope,
        [
            _record("job-expired", "expired"),
            _record("job-deleted", "deleted"),
        ],
        ttl_seconds=10,
        now=100,
    )
    index.mark_deleted(scope, resource_id="job-deleted", now=105)
    index.upsert(
        scope,
        [_record("job-fresh", "fresh")],
        ttl_seconds=10,
        now=105,
    )

    status = index.list_scope_status(now=111)[0]

    assert status.active_count == 1
    assert len(index.lookup(scope, "expired", fresh_only=False, now=111)) == 1
    assert index.lookup(scope, "deleted", fresh_only=False, now=111) == []
    deleted = index.lookup_id(scope, "job-deleted", include_tombstoned=True)
    assert deleted is not None
    assert deleted.tombstoned_at == 105


def test_duplicate_names_are_retained_for_ambiguity_detection(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()
    index.replace_name(
        scope,
        "same",
        [
            _record("job-one", "same", created_at="2026-01-01"),
            _record("job-two", "same", created_at="2026-02-01"),
        ],
        now=100,
    )

    assert [item.resource_id for item in index.lookup(scope, "same", now=101)] == [
        "job-two",
        "job-one",
    ]


def test_mark_deleted_scope_due_purge_and_clear(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()
    index.reconcile(scope, [_record("job-a", "A")], now=100)

    assert index.scope_due(scope, interval_seconds=50, now=149) is False
    assert index.scope_due(scope, interval_seconds=50, now=150) is True
    assert index.mark_deleted(scope, name="A", now=200) == 1
    assert index.purge_tombstones(older_than_seconds=50, now=251) == 1

    index.upsert(scope, [_record("job-b", "B")], now=300)
    index.clear()
    assert index.lookup(scope, "B", fresh_only=False, now=300) == []
    assert index.list_scope_status() == []


def test_clear_generation_rejects_revision_zero_in_flight_refresh(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()
    generation, revision = index.snapshot_token(scope)

    index.clear()

    with pytest.raises(StaleResourceIndexRefresh):
        index.reconcile(
            scope,
            [_record("job-old", "A")],
            expected_generation=generation,
            expected_revision=revision,
        )


def test_prune_orphan_workspace_scopes_removes_only_invisible_workspaces(
    tmp_path,
) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    workspace_scope = _scope(resource_type="workspace", workspace_id="")
    visible_scope = _scope(workspace_id="workspace-visible")
    orphan_scope = _scope(workspace_id="workspace-removed")
    index.reconcile(visible_scope, [_record("job-visible", "visible")], now=100)
    index.reconcile(orphan_scope, [_record("job-old", "old")], now=100)
    generation, workspace_revision, child_revisions = (
        index.snapshot_workspace_refresh(workspace_scope)
    )

    assert (
        index.prune_orphan_workspace_scopes(
            workspace_scope,
            ["workspace-visible"],
            expected_generation=generation,
            expected_workspace_revision=workspace_revision,
            expected_child_revisions=child_revisions,
        )
        == 1
    )

    assert index.lookup(visible_scope, "visible", fresh_only=False)
    assert index.lookup(orphan_scope, "old", fresh_only=False) == []
    assert {
        status.workspace_id for status in index.list_scope_status()
    } == {"workspace-visible"}


def test_prune_orphan_workspace_scopes_preserves_concurrently_changed_scope(
    tmp_path,
) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    workspace_scope = _scope(resource_type="workspace", workspace_id="")
    orphan_scope = _scope(workspace_id="workspace-new")
    index.reconcile(orphan_scope, [_record("job-old", "old")], now=100)
    generation, workspace_revision, child_revisions = (
        index.snapshot_workspace_refresh(workspace_scope)
    )

    index.upsert(orphan_scope, [_record("job-new", "new")], now=101)

    assert (
        index.prune_orphan_workspace_scopes(
            workspace_scope,
            [],
            expected_generation=generation,
            expected_workspace_revision=workspace_revision,
            expected_child_revisions=child_revisions,
        )
        == 0
    )
    assert index.lookup(orphan_scope, "new", fresh_only=False)


def test_prune_orphan_workspace_scopes_rejects_pre_clear_snapshot(
    tmp_path,
) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    workspace_scope = _scope(resource_type="workspace", workspace_id="")
    orphan_scope = _scope(workspace_id="workspace-old")
    index.reconcile(orphan_scope, [_record("job-old", "old")], now=100)
    generation, workspace_revision, child_revisions = (
        index.snapshot_workspace_refresh(workspace_scope)
    )

    index.clear()
    index.upsert(orphan_scope, [_record("job-new", "new")], now=101)

    with pytest.raises(StaleResourceIndexRefresh):
        index.prune_orphan_workspace_scopes(
            workspace_scope,
            [],
            expected_generation=generation,
            expected_workspace_revision=workspace_revision,
            expected_child_revisions=child_revisions,
        )
    assert index.lookup(orphan_scope, "new", fresh_only=False)


def test_refresh_lease_is_single_flight_and_released(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()

    with index.refresh_lease(scope, holder="first", now=100) as first:
        assert first is True
        with index.refresh_lease(scope, holder="second", now=100) as second:
            assert second is False

    with index.refresh_lease(scope, holder="third", now=101) as third:
        assert third is True


def test_refresh_lease_database_errors_are_best_effort(tmp_path, monkeypatch) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()

    def _broken_connect():
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(index, "_connect", _broken_connect)
    with index.refresh_lease(scope) as acquired:
        assert acquired is False


def test_refresh_lease_can_distinguish_database_error_from_contention(
    tmp_path,
    monkeypatch,
) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()

    def _broken_connect():
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(index, "_connect", _broken_connect)
    with pytest.raises(ResourceIndexDatabaseError):
        with index.refresh_lease(scope, raise_on_error=True):
            pass


def test_refresh_lease_release_database_error_does_not_escape(
    tmp_path,
    monkeypatch,
) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()

    with index.refresh_lease(scope, holder="first", now=100) as acquired:
        assert acquired is True

        def _broken_connect():
            raise sqlite3.OperationalError("database unavailable")

        monkeypatch.setattr(index, "_connect", _broken_connect)


def test_refresh_error_and_purge_database_errors_preserve_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()
    index.reconcile(
        scope,
        [_record("job-live", "live"), _record("job-deleted", "deleted")],
        now=100,
    )
    index.mark_deleted(scope, resource_id="job-deleted", now=100)

    def _broken_connect():
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(index, "_connect", _broken_connect)
    index.record_refresh_error(scope, "temporary failure")
    assert index.purge_tombstones(now=100) == 0

    restored = ResourceIndex(tmp_path / "index.sqlite3")
    live = restored.lookup(scope, "live", fresh_only=False, now=100)
    assert [item.resource_id for item in live] == ["job-live"]
    deleted = restored.lookup_id(scope, "job-deleted", include_tombstoned=True)
    assert deleted is not None
    assert deleted.tombstoned_at == 100


def test_refresh_lease_renews_during_a_long_scan(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()

    with index.refresh_lease(scope, holder="first", lease_seconds=1) as first:
        assert first is True
        time.sleep(0.75)
        with index.refresh_lease(
            scope,
            holder="second",
            lease_seconds=1,
        ) as second:
            assert second is False


def test_corrupt_database_is_discarded_and_rebuilt(tmp_path) -> None:
    path = tmp_path / "index.sqlite3"
    path.write_bytes(b"not a sqlite database")
    (tmp_path / "index.sqlite3-wal").write_bytes(b"stale wal")
    (tmp_path / "index.sqlite3-shm").write_bytes(b"stale shm")

    index = ResourceIndex(path)

    assert index.list_scope_status() == []
    assert path.exists()


@pytest.mark.parametrize(
    "message",
    [
        "database is locked",
        "attempt to write a readonly database",
        "disk I/O error",
    ],
)
def test_non_corruption_database_errors_are_not_discarded(message: str) -> None:
    assert ResourceIndex._is_corruption_error(sqlite3.OperationalError(message)) is False


def test_scope_for_session_requires_stable_account_identity() -> None:
    class Session:
        base_url = "https://inspire.example/"
        login_username = "alice"
        user_detail = None

    assert scope_for_session(Session(), resource_type="job") == ResourceScope(
        base_url="https://inspire.example",
        subject_id="login:alice",
        resource_type="job",
    )

    class AnonymousSession:
        base_url = "https://inspire.example"
        login_username = None
        user_detail = None

    assert scope_for_session(AnonymousSession(), resource_type="job") is None


def test_account_indexes_are_isolated_and_private(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    create_account("alpha", "[inspire]\n")
    create_account("beta", "[inspire]\n")

    set_current_account("alpha")
    alpha_path = resource_index_path()
    alpha = ResourceIndex.for_account()
    assert alpha_path is not None
    assert alpha is not None
    alpha.upsert(_scope(), [_record("job-alpha", "train")], now=100)

    set_current_account("beta")
    beta_path = resource_index_path()
    beta = ResourceIndex.for_account()
    assert beta_path is not None
    assert beta is not None
    assert beta_path != alpha_path
    assert beta.lookup(_scope(), "train", fresh_only=False, now=100) == []

    if os.name != "nt":
        assert alpha_path.stat().st_mode & 0o777 == 0o600
        assert alpha_path.parent.stat().st_mode & 0o777 == 0o700


def test_scope_isolates_base_url_subject_workspace_and_owner(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scopes = [
        ResourceScope(
            base_url="https://inspire.example",
            subject_id="user-one",
            resource_type="job",
            workspace_id="workspace-one",
            owner_scope="self",
        ),
        ResourceScope(
            base_url="https://other.example",
            subject_id="user-one",
            resource_type="job",
            workspace_id="workspace-one",
            owner_scope="self",
        ),
        ResourceScope(
            base_url="https://inspire.example",
            subject_id="user-two",
            resource_type="job",
            workspace_id="workspace-one",
            owner_scope="self",
        ),
        ResourceScope(
            base_url="https://inspire.example",
            subject_id="user-one",
            resource_type="job",
            workspace_id="workspace-two",
            owner_scope="self",
        ),
        ResourceScope(
            base_url="https://inspire.example",
            subject_id="user-one",
            resource_type="job",
            workspace_id="workspace-one",
            owner_scope="team",
        ),
    ]
    for number, scope in enumerate(scopes):
        index.upsert(scope, [_record(f"job-{number}", "train")])

    for number, scope in enumerate(scopes):
        assert [item.resource_id for item in index.lookup(scope, "train")] == [
            f"job-{number}"
        ]


def test_empty_targeted_name_is_rejected(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    with pytest.raises(ValueError, match="cannot be empty"):
        index.replace_name(_scope(), " ", [])
