"""Tests for inspire.cli.utils.id_resolver."""

from __future__ import annotations

import sqlite3

import pytest

from inspire.cli.context import Context
from inspire.cli.utils.id_resolver import (
    forget_resource_identity,
    is_full_uuid,
    is_stale_handle_error,
    is_partial_id,
    looks_like_platform_id,
    remember_resource_identity,
    resolve_by_name,
    run_with_stale_handle_retry,
)
from inspire.cli.utils.resource_index import ResourceIdentity, ResourceIndex, ResourceScope


# ---------------------------------------------------------------------------
# is_full_uuid
# ---------------------------------------------------------------------------


class TestIsFullUuid:
    def test_standard_uuid(self):
        assert is_full_uuid("c4eb3ac3-6d83-405c-aa29-059bc945c4bf") is True

    def test_uppercase_uuid(self):
        assert is_full_uuid("C4EB3AC3-6D83-405C-AA29-059BC945C4BF") is True

    def test_with_prefix(self):
        assert is_full_uuid("job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf", prefix="job-") is True

    def test_prefix_case_insensitive(self):
        assert is_full_uuid("JOB-c4eb3ac3-6d83-405c-aa29-059bc945c4bf", prefix="job-") is True

    def test_uuid_without_matching_prefix(self):
        # "job-" prefix present but we strip "notebook-" — leaves "job-..." which is not a UUID
        assert is_full_uuid("job-c4eb3ac3-6d83-405c-aa29-059bc945c4bf", prefix="notebook-") is False

    def test_partial_hex_not_full(self):
        assert is_full_uuid("c4eb3ac3") is False

    def test_empty(self):
        assert is_full_uuid("") is False

    def test_whitespace_stripped(self):
        assert is_full_uuid("  c4eb3ac3-6d83-405c-aa29-059bc945c4bf  ") is True


# ---------------------------------------------------------------------------
# is_partial_id
# ---------------------------------------------------------------------------


class TestIsPartialId:
    def test_four_hex_chars(self):
        assert is_partial_id("abcd") is True

    def test_eight_hex_chars(self):
        assert is_partial_id("c4eb3ac3") is True

    def test_too_short(self):
        assert is_partial_id("abc") is False

    def test_full_uuid_not_partial(self):
        assert is_partial_id("c4eb3ac3-6d83-405c-aa29-059bc945c4bf") is False

    def test_non_hex(self):
        assert is_partial_id("mynotebook") is False

    def test_with_prefix(self):
        assert is_partial_id("job-c4eb3ac3", prefix="job-") is True

    def test_prefix_stripped_too_short(self):
        assert is_partial_id("job-ab", prefix="job-") is False

    def test_mixed_case_hex(self):
        assert is_partial_id("AbCd1234") is True

    def test_empty(self):
        assert is_partial_id("") is False

    def test_hex_with_hyphens_no_prefix(self):
        # "abcd-1234" is not pure hex (has hyphens), but not a full UUID
        assert is_partial_id("abcd-1234") is False

    def test_long_hex_not_uuid_format(self):
        # 32 hex chars without hyphens — partial, not a full UUID
        assert is_partial_id("c4eb3ac36d83405caa29059bc945c4bf") is True


@pytest.mark.parametrize("name", ["2026", "cafe", "deadbeef", "face"])
def test_bare_hex_names_are_not_rejected_as_platform_ids(name: str) -> None:
    assert looks_like_platform_id(name) is False


@pytest.mark.parametrize(
    "value",
    [
        "ws-1",
        "cg-1",
        "lcg-1",
        "group-123456",
        "compute-group-abcdef",
        "workspace-abcdef",
        "proj-123456",
    ],
)
def test_compute_and_workspace_handles_are_rejected(value: str) -> None:
    assert looks_like_platform_id(value) is True


class _FakeContext:
    """Minimal stand-in for Context."""

    def __init__(self, json_output: bool = False):
        self.json_output = json_output


def _scope() -> ResourceScope:
    return ResourceScope(
        base_url="https://inspire.example",
        subject_id="user-one",
        resource_type="job",
        workspace_id="workspace-one",
    )


class TestResolveByName:
    def test_handle_shaped_error_omits_id_hint_by_default(self, capsys):
        ctx = _FakeContext(json_output=True)

        with pytest.raises(SystemExit):
            resolve_by_name(
                ctx,
                name="image-c4eb3ac3-6d83-405c-aa29-059bc945c4bf",
                resource_type="image",
                list_candidates=lambda: [],
                json_output=True,
            )

        captured = capsys.readouterr()
        assert "inspire image list" in captured.err
        assert "dedicated `id` command" not in captured.err

    def test_date_suffixed_names_are_not_treated_as_handles(self):
        ctx = _FakeContext(json_output=True)

        result = resolve_by_name(
            ctx,
            name="job-smoke-20260507",
            resource_type="job",
            list_candidates=lambda: [{"name": "job-smoke-20260507", "id": "job-id"}],
            json_output=True,
        )

        assert result == "job-id"

    @pytest.mark.parametrize(
        "name, resource_type",
        [
            ("hpc-job-123", "hpc"),
            ("rj-abc", "ray"),
            ("ray-abc-1", "ray"),
            ("img-001", "image"),
            ("image-abc-def", "image"),
        ],
    )
    def test_compact_platform_handles_are_rejected_before_listing(
        self,
        name: str,
        resource_type: str,
        capsys,
    ):
        ctx = _FakeContext(json_output=True)

        def _fail_lister():
            raise AssertionError("compact handle should be rejected before listing")

        with pytest.raises(SystemExit):
            resolve_by_name(
                ctx,
                name=name,
                resource_type=resource_type,
                list_candidates=_fail_lister,
                json_output=True,
            )

        assert f"{resource_type} name" in capsys.readouterr().err

    def test_fresh_cache_hit_skips_live_listing(self, tmp_path):
        ctx = _FakeContext(json_output=True)
        index = ResourceIndex(tmp_path / "index.sqlite3")
        scope = ResourceScope(
            base_url="https://inspire.example",
            subject_id="user-one",
            resource_type="job",
            workspace_id="workspace-one",
        )
        index.upsert(
            scope,
            [ResourceIdentity(resource_id="job-cached", name="train")],
        )

        result = resolve_by_name(
            ctx,
            name="train",
            resource_type="job",
            list_candidates=lambda: pytest.fail("fresh cache should avoid a live list"),
            cache_index=index,
            cache_scope=scope,
        )

        assert result == "job-cached"

    def test_require_live_replaces_deleted_and_recreated_resource(self, tmp_path):
        ctx = _FakeContext(json_output=True)
        index = ResourceIndex(tmp_path / "index.sqlite3")
        scope = ResourceScope(
            base_url="https://inspire.example",
            subject_id="user-one",
            resource_type="notebook",
            workspace_id="workspace-one",
        )
        index.upsert(
            scope,
            [ResourceIdentity(resource_id="notebook-old", name="A")],
        )

        result = resolve_by_name(
            ctx,
            name="A",
            resource_type="notebook",
            list_candidates=lambda: [{"name": "A", "id": "notebook-new"}],
            cache_index=index,
            cache_scope=scope,
            require_live=True,
        )

        assert result == "notebook-new"
        assert [item.resource_id for item in index.lookup(scope, "A")] == [
            "notebook-new"
        ]
        old = index.lookup_id(scope, "notebook-old", include_tombstoned=True)
        assert old is not None
        assert old.tombstoned_at is not None

    def test_complete_scope_refresh_tombstones_unseen_names(self, tmp_path):
        ctx = _FakeContext(json_output=True)
        index = ResourceIndex(tmp_path / "index.sqlite3")
        scope = ResourceScope(
            base_url="https://inspire.example",
            subject_id="user-one",
            resource_type="project",
            workspace_id="workspace-one",
        )
        index.reconcile(
            scope,
            [
                ResourceIdentity(resource_id="project-a", name="A"),
                ResourceIdentity(resource_id="project-b", name="B"),
            ],
        )

        result = resolve_by_name(
            ctx,
            name="A",
            resource_type="project",
            list_candidates=lambda: [{"name": "A", "id": "project-a"}],
            cache_index=index,
            cache_scope=scope,
            require_live=True,
            reconcile_scope=True,
        )

        assert result == "project-a"
        assert index.lookup(scope, "B", fresh_only=False) == []

    def test_clear_during_live_lookup_does_not_repopulate_cache(self, tmp_path):
        ctx = _FakeContext(json_output=True)
        index = ResourceIndex(tmp_path / "index.sqlite3")
        scope = _scope()

        def _live_candidates():
            index.clear()
            return [{"name": "train", "id": "job-live"}]

        result = resolve_by_name(
            ctx,
            name="train",
            resource_type="job",
            list_candidates=_live_candidates,
            cache_index=index,
            cache_scope=scope,
            require_live=True,
        )

        assert result == "job-live"
        assert index.list_identities(scope, fresh_only=False) == []

    def test_clear_during_live_reconcile_does_not_repopulate_cache(self, tmp_path):
        ctx = _FakeContext(json_output=True)
        index = ResourceIndex(tmp_path / "index.sqlite3")
        scope = ResourceScope(
            base_url="https://inspire.example",
            subject_id="user-one",
            resource_type="project",
            workspace_id="workspace-one",
        )

        def _live_candidates():
            index.clear()
            return [{"name": "A", "id": "project-live"}]

        result = resolve_by_name(
            ctx,
            name="A",
            resource_type="project",
            list_candidates=_live_candidates,
            cache_index=index,
            cache_scope=scope,
            require_live=True,
            reconcile_scope=True,
        )

        assert result == "project-live"
        assert index.list_identities(scope, fresh_only=False) == []

    def test_snapshot_failure_skips_live_cache_write(
        self,
        tmp_path,
        monkeypatch,
    ):
        ctx = _FakeContext(json_output=True)
        index = ResourceIndex(tmp_path / "index.sqlite3")
        scope = _scope()
        monkeypatch.setattr(
            index,
            "snapshot_token",
            lambda _scope: (_ for _ in ()).throw(OSError("cache unavailable")),
        )

        result = resolve_by_name(
            ctx,
            name="train",
            resource_type="job",
            list_candidates=lambda: [{"name": "train", "id": "job-live"}],
            cache_index=index,
            cache_scope=scope,
            require_live=True,
        )

        assert result == "job-live"
        assert index.list_identities(scope, fresh_only=False) == []

    def test_corrupt_cache_falls_back_to_live_lookup(self):
        ctx = _FakeContext(json_output=True)
        scope = _scope()

        class CorruptCache:
            def lookup(self, *_args, **_kwargs):
                raise sqlite3.DatabaseError("database disk image is malformed")

            def snapshot_token(self, *_args, **_kwargs):
                raise sqlite3.DatabaseError("database disk image is malformed")

        result = resolve_by_name(
            ctx,
            name="train",
            resource_type="job",
            list_candidates=lambda: [{"name": "train", "id": "job-live"}],
            cache_index=CorruptCache(),  # type: ignore[arg-type]
            cache_scope=scope,
            require_live=True,
        )

        assert result == "job-live"


def test_write_through_helpers_update_and_tombstone(tmp_path) -> None:
    class Session:
        base_url = "https://inspire.example"
        user_detail = {"id": "user-one"}
        login_username = "alice"

    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = ResourceScope(
        base_url="https://inspire.example",
        subject_id="user-one",
        resource_type="model",
        workspace_id="workspace-one",
    )

    remember_resource_identity(
        session=Session(),
        resource_type="model",
        resource_id="model-one",
        name="demo",
        workspace_id="workspace-one",
        cache_index=index,
    )
    assert [item.resource_id for item in index.lookup(scope, "demo")] == ["model-one"]

    forget_resource_identity(
        session=Session(),
        resource_type="model",
        resource_id="model-one",
        name="demo",
        workspace_id="workspace-one",
        cache_index=index,
    )
    assert index.lookup(scope, "demo", fresh_only=False) == []


def test_live_name_snapshot_cannot_overwrite_newer_write_through(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope()
    index.upsert(
        scope,
        [ResourceIdentity(resource_id="job-old", name="demo")],
    )

    def _stale_live_list():
        index.mark_deleted(scope, resource_id="job-old")
        index.upsert(
            scope,
            [ResourceIdentity(resource_id="job-new", name="demo")],
        )
        return [{"id": "job-old", "name": "demo"}]

    resolved = resolve_by_name(
        Context(),
        name="demo",
        resource_type="job",
        list_candidates=_stale_live_list,
        cache_index=index,
        cache_scope=scope,
        require_live=True,
    )

    assert resolved == "job-new"
    assert [
        item.resource_id
        for item in index.lookup(scope, "demo", fresh_only=False)
    ] == ["job-new"]


def test_is_stale_handle_error_accepts_explicit_not_found_signals() -> None:
    class NotFoundResponseError(Exception):
        status_code = 404

    assert is_stale_handle_error(NotFoundResponseError("request failed")) is True
    assert is_stale_handle_error(RuntimeError("resource not found")) is True
    assert is_stale_handle_error(RuntimeError("invalid resource id")) is True
    assert is_stale_handle_error(RuntimeError("invalid-job-id")) is True


def test_is_stale_handle_error_rejects_auth_status_even_with_not_found_text() -> None:
    class UnauthorizedResponseError(Exception):
        status_code = 401

    assert is_stale_handle_error(
        UnauthorizedResponseError("resource not found")
    ) is False


def test_is_stale_handle_error_rejects_auth_named_404_errors() -> None:
    class AuthenticationError(Exception):
        status_code = 404

    assert is_stale_handle_error(AuthenticationError("resource not found")) is False


@pytest.mark.parametrize(
    "error",
    (
        TimeoutError("resource not found after timeout"),
        RuntimeError("HTTP 503: service unavailable"),
        RuntimeError("authentication failed: token expired"),
        RuntimeError("invalid credentials; resource not found"),
    ),
)
def test_is_stale_handle_error_rejects_transient_and_auth_failures(error: Exception) -> None:
    assert is_stale_handle_error(error) is False


def test_stale_handle_retry_invalidates_exact_old_handle_and_resolves_live() -> None:
    calls: list[object] = []
    operations = {"old-handle": 0, "new-handle": 0}

    def resolve_cached() -> str:
        calls.append("resolve_cached")
        return "old-handle"

    def resolve_live(name: str) -> str:
        calls.append(("resolve_live", name))
        return "new-handle"

    def operation(handle: str) -> str:
        calls.append(("operation", handle))
        operations[handle] += 1
        if handle == "old-handle":
            raise RuntimeError("404 resource not found")
        return "deleted"

    def invalidate(handle: str) -> None:
        calls.append(("invalidate", handle))

    result = run_with_stale_handle_retry(
        name="demo",
        resolve_cached=resolve_cached,
        resolve_live=resolve_live,
        operation=operation,
        invalidate=invalidate,
    )

    assert result == "deleted"
    assert calls == [
        "resolve_cached",
        ("operation", "old-handle"),
        ("invalidate", "old-handle"),
        ("resolve_live", "demo"),
        ("operation", "new-handle"),
    ]
    assert operations == {"old-handle": 1, "new-handle": 1}


def test_stale_handle_invalidation_does_not_fallback_after_clear_recreate(
    tmp_path,
) -> None:
    class Session:
        base_url = "https://inspire.example"
        user_detail = {"id": "user-one"}

    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = ResourceScope(
        base_url="https://inspire.example",
        subject_id="user-one",
        resource_type="model",
        workspace_id="workspace-one",
    )
    index.upsert(scope, [ResourceIdentity("model-old", "demo")])
    index.clear()
    index.upsert(scope, [ResourceIdentity("model-new", "demo")])

    def operation(handle: str) -> str:
        if handle == "model-old":
            raise RuntimeError("404 resource not found")
        return "ok"

    result = run_with_stale_handle_retry(
        name="demo",
        resolve_cached=lambda: "model-old",
        resolve_live=lambda _name: "model-new",
        operation=operation,
        invalidate=lambda handle: forget_resource_identity(
            session=Session(),
            resource_type="model",
            resource_id=handle,
            name="demo",
            workspace_id="workspace-one",
            cache_index=index,
        ),
    )

    assert result == "ok"
    replacement = index.lookup_id(scope, "model-new")
    assert replacement is not None
    assert replacement.tombstoned_at is None


@pytest.mark.parametrize(
    "error",
    (
        TimeoutError("request timed out"),
        RuntimeError("HTTP 500 server error"),
        RuntimeError("authentication failed"),
    ),
)
def test_stale_handle_retry_does_not_repeat_non_stale_failures(error: Exception) -> None:
    calls: list[object] = []

    def operation(handle: str) -> None:
        calls.append(handle)
        raise error

    with pytest.raises(type(error), match=str(error)):
        run_with_stale_handle_retry(
            name="demo",
            resolve_cached=lambda: "cached-handle",
            resolve_live=lambda _name: pytest.fail("non-stale error must not resolve live"),
            operation=operation,
            invalidate=lambda _handle: pytest.fail("non-stale error must not invalidate"),
        )

    assert calls == ["cached-handle"]


def test_stale_handle_retry_does_not_retry_a_second_stale_failure() -> None:
    calls: list[object] = []

    def operation(handle: str) -> None:
        calls.append(handle)
        raise RuntimeError("not found")

    with pytest.raises(RuntimeError, match="not found"):
        run_with_stale_handle_retry(
            name="demo",
            resolve_cached=lambda: "old-handle",
            resolve_live=lambda name: "new-handle",
            operation=operation,
            invalidate=lambda handle: calls.append(("invalidate", handle)),
        )

    assert calls == [
        "old-handle",
        ("invalidate", "old-handle"),
        "new-handle",
    ]


def test_stale_handle_retry_survives_cache_invalidation_failure() -> None:
    calls: list[str] = []

    def operation(handle: str) -> str:
        calls.append(handle)
        if handle == "old-handle":
            raise RuntimeError("404 resource not found")
        return "ok"

    assert (
        run_with_stale_handle_retry(
            name="demo",
            resolve_cached=lambda: "old-handle",
            resolve_live=lambda _name: "new-handle",
            operation=operation,
            invalidate=lambda _handle: (_ for _ in ()).throw(
                OSError("cache unavailable")
            ),
        )
        == "ok"
    )
    assert calls == ["old-handle", "new-handle"]
