"""Tests for inspire.cli.utils.id_resolver."""

from __future__ import annotations

import pytest

from inspire.cli.utils.id_resolver import (
    forget_resource_identity,
    is_full_uuid,
    is_partial_id,
    remember_resource_identity,
    resolve_by_name,
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


class _FakeContext:
    """Minimal stand-in for Context."""

    def __init__(self, json_output: bool = False):
        self.json_output = json_output


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
