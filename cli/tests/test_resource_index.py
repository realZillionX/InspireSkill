from __future__ import annotations

import os
import time

import pytest

from inspire.accounts import create_account, set_current_account
from inspire.cli.utils.resource_index import (
    ResourceIdentity,
    ResourceIndex,
    ResourceScope,
    candidates_from_dicts,
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


def test_refresh_error_preserves_last_successful_snapshot(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()
    index.reconcile(scope, [_record("job-a", "A")], now=100)

    index.record_refresh_error(scope, "network unavailable", now=200)

    status = index.list_scope_status()[0]
    assert status.last_attempt_at == 200
    assert status.last_refresh_at == 100
    assert status.last_full_refresh_at == 100
    assert status.refresh_complete is True
    assert status.last_error == "network unavailable"
    assert [
        item.resource_id
        for item in index.lookup(scope, "A", fresh_only=False, now=200)
    ] == ["job-a"]


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


def test_refresh_lease_is_single_flight_and_released(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()

    with index.refresh_lease(scope, holder="first", now=100) as first:
        assert first is True
        with index.refresh_lease(scope, holder="second", now=100) as second:
            assert second is False

    with index.refresh_lease(scope, holder="third", now=101) as third:
        assert third is True


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


def test_candidates_from_dicts_keeps_only_minimal_identity_fields() -> None:
    records = candidates_from_dicts(
        [
            {
                "handle": "job-one",
                "display": "train",
                "created_by_id": "user-one",
                "status": "RUNNING",
                "created_at": "now",
                "raw": {"large": "payload"},
            },
            {"handle": "", "display": "missing"},
        ],
        name_key="display",
        id_key="handle",
    )

    assert records == [
        ResourceIdentity(
            resource_id="job-one",
            name="train",
            owner_id="user-one",
            status="RUNNING",
            created_at="now",
        )
    ]


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


def test_empty_targeted_name_is_rejected(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    with pytest.raises(ValueError, match="cannot be empty"):
        index.replace_name(_scope(), " ", [])
