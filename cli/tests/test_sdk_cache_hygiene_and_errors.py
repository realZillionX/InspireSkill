"""Cache hygiene and public error regressions, entirely offline."""

from collections import Counter
from types import SimpleNamespace
import importlib
import sqlite3
from contextlib import closing
import time

import pytest

from test_sdk import client as client
from test_sdk_resources import catalog as catalog
from test_sdk_cache import catalog as _job_catalog

from inspire.sdk import InspireAsyncClient, InspireError, ValidationError, iter_output_file
from inspire.sdk.identity_cache import IdentityCache
from inspire.services.catalog.resource_index import ResourceIdentity, ResourceIndex
from inspire.services.catalog.resource_refresh import FetchResult, refresh_scope


job_catalog = _job_catalog


def setup_cache(tmp_path):
    session = SimpleNamespace(
        base_url="https://example.invalid", login_username="fake", user_detail=None
    )
    cache = IdentityCache("fake", 60, session.base_url)
    cache._index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = cache.scope(session, "compute_groups", ("ws",))
    return session, cache, scope


@pytest.mark.parametrize("kind", ["compute_groups", "workspaces", "projects"])
def test_identity_only_scope_heals_once_per_cli_write(tmp_path, kind):
    session, cache, scope = setup_cache(tmp_path)
    index = cache.index()
    from inspire.platform.web.browser_api.projects import ProjectInfo

    scope = cache.scope(session, kind, ("ws",))
    calls = Counter()

    def load():
        calls["load"] += 1
        if kind == "projects":
            return [ProjectInfo(project_id="g", name="Group", workspace_id="ws")]
        return [{"id": "g", "name": "Group"}]

    for expected in (1, 2):
        result = refresh_scope(
            index=index,
            session=session,
            scope=scope,
            resource_type=scope.resource_type,
            workspace_id="ws",
            workspace_name="",
            exact_name="",
            force=True,
            fetcher=lambda *args: FetchResult([ResourceIdentity("g", "Group")]),
        )
        assert result.outcome == "refreshed"
        assert cache.get(session, kind, ("ws",), load)[1] is False
        assert all(row.payload for row in index.list_identities(scope))
        assert cache.get(session, kind, ("ws",), load)[1] is True
        assert calls["load"] == expected


def test_sdk_only_refresh_purges_old_tombstones(tmp_path):
    session, cache, scope = setup_cache(tmp_path)
    index = cache.index()
    old = time.time() - 10 * 86400
    index.reconcile(scope, [ResourceIdentity("old", "Old")], now=old)
    index.reconcile(scope, [], now=old + 1)
    cache.get(session, "compute_groups", ("ws",), lambda: [{"id": "g", "name": "Group"}])
    assert index.lookup_id(scope, "old", include_tombstoned=True) is None


def test_status_distinct_images_workspaces_and_size(tmp_path, monkeypatch):
    from inspire.cli.commands.cache import _status_payload
    from inspire.services.catalog.resource_index import ResourceScope

    index = ResourceIndex(tmp_path / "status.sqlite3")
    monkeypatch.setattr(
        importlib.import_module("inspire.cli.commands.cache"),
        "_workspace_name_map",
        lambda: {"ws": "Workspace"},
    )
    for source in ("self", "official", "public", "project", "private"):
        index.reconcile(
            ResourceScope("https://example.invalid", "fake", "image", "ws", source),
            [ResourceIdentity(str(i), f"Image {i}") for i in range(10)],
        )
    payload = _status_payload(index, resources=("image",))
    row = payload["items"][0]
    assert row["cached_names"] == 10
    assert row["workspaces"] == 1
    assert row["scopes"] == 5
    assert payload["size_bytes"] >= index.path.stat().st_size > 0


@pytest.mark.parametrize("name", ["account", "base_url"])
def test_async_properties_are_inspire_errors(name):
    client = InspireAsyncClient()
    with pytest.raises(InspireError, match="first operation.*async with"):
        getattr(client, name)


def test_output_chunk_validation_is_inspire_error(tmp_path):
    with pytest.raises(ValidationError, match="chunk_size"):
        list(iter_output_file(tmp_path / "unused", chunk_size=0))


def test_api_key_uncertainty_names_catalog(client, catalog, monkeypatch):
    from inspire.sdk import SubmissionUncertainError

    def fail(*args, **kwargs):
        client._transport._write["sent"] = True
        raise ValueError("response lost")

    monkeypatch.setattr(client._transport, "_once", fail)
    with pytest.raises(SubmissionUncertainError, match="inspect API keys"):
        client.api_keys.create("new")


@pytest.mark.parametrize("specs", [["bad"], ["a:1", "a:1"]])
def test_dataset_sdk_wording(client, specs):
    with pytest.raises(ValidationError, match="^specs "):
        client.datasets.validate(specs, workspace="unused")


def test_exec_timeout_names_parameter(client):
    with pytest.raises(ValidationError, match="^timeout "):
        client.notebooks.exec("unused", command="true", timeout=0)


def test_transfer_deadline_is_inspire_error(monkeypatch):
    from inspire.services.execution import notebook_transfer as core

    monkeypatch.setattr(core, "load_tunnel_config", lambda **kw: None)
    with pytest.raises(InspireError, match="SSH transfer timed out"):
        core.transfer_ssh(
            local="unused",
            remote="/tmp/unused",
            download=False,
            recursive=False,
            overwrite=False,
            bridge_name="fake",
            account="fake",
            timeout=-1,
        )


@pytest.mark.parametrize(
    "changes, parameter",
    [
        ({"shm_gib": 100000}, "shm_gib"),
        (
            {"auto_fault_tolerance": False, "fault_tolerance_retry_interval_sec": 5},
            "fault_tolerance_retry_interval_sec",
        ),
    ],
)
def test_job_shared_validation_uses_sdk_parameters(client, job_catalog, changes, parameter):
    from dataclasses import replace

    with pytest.raises(ValidationError) as error:
        client.jobs.plan(replace(job_catalog.spec, **changes))
    assert parameter in str(error.value)
    assert "--" not in str(error.value)


def test_image_save_hint_names_image_catalog(client):
    from inspire.sdk import ImageSaveHandle, NotebookRef

    with pytest.raises(ValidationError, match="image catalog"):
        client.notebooks.wait_image_ready(
            ImageSaveHandle(
                "image", None, NotebookRef("Notebook", client.account, client.base_url, "nb", "ws")
            )
        )


@pytest.mark.parametrize("content", [None, b"\xff"])
def test_output_file_read_failures_are_permanent_not_retryable(tmp_path, content):
    """An absent or undecodable capture cannot be fixed by calling again."""
    path = tmp_path / "capture"
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(ValidationError) as raised:
        list(iter_output_file(path))
    assert raised.value.retryable is False


def test_upload_local_io_failure_is_inspire_error(client, tmp_path):
    with pytest.raises(InspireError):
        client.notebooks.upload("unused", local=tmp_path / "absent", remote="file")


@pytest.mark.parametrize("legacy", [False, True])
def test_purge_reclaims_incremental_pages_without_rewriting_legacy(tmp_path, legacy):
    from inspire.services.catalog.resource_index import ResourceScope

    path = tmp_path / "pages.sqlite3"
    if legacy:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE legacy_marker (value TEXT)")
            connection.commit()
    index = ResourceIndex(path)
    scope = ResourceScope("https://example.invalid", "fake", "image", "ws", "self")
    index.reconcile(
        scope, [ResourceIdentity(str(i), str(i), payload="x" * 8192) for i in range(100)], now=1
    )
    index.reconcile(scope, [], now=2)
    with closing(sqlite3.connect(path)) as connection:
        before = connection.execute("PRAGMA page_count").fetchone()[0]
        assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == (0 if legacy else 2)
    assert index.purge_tombstones(now=1e9) == 100
    with closing(sqlite3.connect(path)) as connection:
        after = connection.execute("PRAGMA page_count").fetchone()[0]
        assert after == before if legacy else after < before
        if legacy:
            assert connection.execute("SELECT * FROM legacy_marker").fetchall() == []


def test_sdk_workspace_refresh_prunes_orphans(tmp_path):
    from inspire.services.catalog.resource_index import ResourceScope

    session, cache, _ = setup_cache(tmp_path)
    index = cache.index()
    child = ResourceScope(session.base_url, "login:fake", "image", "gone", "self")
    index.reconcile(child, [ResourceIdentity("old", "Old")])
    cache.get(session, "workspaces", (), lambda: [{"id": "ws", "name": "Workspace"}])
    assert all(status.workspace_id != "gone" for status in index.list_scope_status())


@pytest.mark.parametrize(
    "method", ["purge_tombstones", "prune_orphan_workspace_scopes", "snapshot_workspace_refresh"]
)
def test_maintenance_failure_preserves_published_sdk_snapshot(tmp_path, monkeypatch, method):
    session, cache, _ = setup_cache(tmp_path)
    index = cache.index()

    def fail(*args, **kwargs):
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(index, method, fail)
    value = [{"id": "ws", "name": "Workspace"}]
    assert cache.get(session, "workspaces", (), lambda: value) == (value, False)
    assert cache.get(session, "workspaces", (), lambda: pytest.fail("unexpected live fetch")) == (
        value,
        True,
    )


def test_busy_payload_repair_does_not_publish(tmp_path):
    session, cache, scope = setup_cache(tmp_path)
    index = cache.index()
    index.reconcile(scope, [ResourceIdentity("g", "Group")])
    with index.refresh_lease(scope) as acquired:
        assert acquired
        assert (
            cache.get(session, "compute_groups", ("ws",), lambda: [{"id": "g", "name": "Group"}])[1]
            is False
        )
        assert index.list_identities(scope)[0].payload == ""


def test_failed_payload_repair_keeps_identity_snapshot(tmp_path):
    from inspire.sdk import ResolutionIncompleteError

    session, cache, scope = setup_cache(tmp_path)
    index = cache.index()
    index.reconcile(scope, [ResourceIdentity("g", "Group")])

    def fail():
        raise ResolutionIncompleteError("missing page")

    with pytest.raises(ResolutionIncompleteError):
        cache.get(session, "compute_groups", ("ws",), fail)
    assert [r.resource_id for r in index.list_identities(scope)] == ["g"]


@pytest.mark.parametrize(
    "method, kwargs, parameter",
    [
        ("wait", {"timeout": 0}, "timeout"),
        ("wait", {"poll_interval": float("nan")}, "poll_interval"),
        ("follow_events", {"interval": -1}, "interval"),
        ("upload", {"local": "unused", "remote": "unused", "timeout": 0}, "timeout"),
    ],
)
def test_notebook_duration_names_rejected_parameter(client, method, kwargs, parameter):
    with pytest.raises(ValidationError, match=f"^{parameter} "):
        result = getattr(client.notebooks, method)("unused", **kwargs)
        if method == "follow_events":
            next(result)


def test_shared_dataset_cli_wording_unchanged():
    from inspire.services.catalog.datasets import DatasetSpecError, parse_dataset_specs

    with pytest.raises(DatasetSpecError, match="^--dataset expects"):
        parse_dataset_specs(["bad"])
    with pytest.raises(DatasetSpecError, match="^--dataset a:1 was given more than once$"):
        parse_dataset_specs(["a:1", "a:1"])


@pytest.mark.parametrize(
    "payload", ["not json", '["unknown", {}]', '"scalar"', '["ProjectInfo", []]']
)
def test_undecodable_scope_is_repaired_without_weakening_payload_check(tmp_path, payload):
    session, cache, scope = setup_cache(tmp_path)
    index = cache.index()
    index.reconcile(scope, [ResourceIdentity("g", "Group", payload=payload)])
    value = [{"id": "g", "name": "Group"}]
    assert cache.get(session, "compute_groups", ("ws",), lambda: value) == (value, False)
    assert cache.get(
        session, "compute_groups", ("ws",), lambda: pytest.fail("unexpected fetch")
    ) == (value, True)


def test_repair_rechecks_after_lease_acquisition(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from inspire.services.catalog.catalog_codec import encode_catalog
    import json

    session, cache, scope = setup_cache(tmp_path)
    index = cache.index()
    index.reconcile(scope, [ResourceIdentity("g", "Group")])
    lease = index.refresh_lease
    value = [{"id": "g", "name": "Group"}]

    @contextmanager
    def after_other_writer(scope, **kwargs):
        index.reconcile(
            scope, [ResourceIdentity("g", "Group", payload=json.dumps(encode_catalog(value[0])))]
        )
        with lease(scope, **kwargs) as acquired:
            yield acquired

    monkeypatch.setattr(index, "refresh_lease", after_other_writer)
    assert cache.get(
        session, "compute_groups", ("ws",), lambda: pytest.fail("already repaired")
    ) == (value, True)


def test_distinct_counts_ignore_expired_tombstoned_and_other_subjects(tmp_path, monkeypatch):
    from inspire.services.catalog.resource_index import ResourceScope

    module = importlib.import_module("inspire.cli.commands.cache")
    monkeypatch.setattr(module, "_workspace_name_map", lambda: {})
    index = ResourceIndex(tmp_path / "counts.sqlite3")
    for workspace in ("a", "b", "c"):
        index.reconcile(
            ResourceScope("https://example.invalid", "fake", "image", workspace, "self"),
            [ResourceIdentity("shared", "Image")],
        )
    index.reconcile(
        ResourceScope("https://example.invalid", "other", "image", "d", "self"),
        [ResourceIdentity("unrelated", "Other")],
    )
    stale = ResourceScope("https://example.invalid", "fake", "image", "a", "public")
    index.reconcile(stale, [ResourceIdentity("expired", "Expired")], now=1)
    deleted = ResourceScope("https://example.invalid", "fake", "image", "a", "private")
    index.reconcile(deleted, [ResourceIdentity("deleted", "Deleted")])
    index.reconcile(deleted, [])
    row = module._status_payload(
        index, resources=("image",), base_url="https://example.invalid", subject_id="fake"
    )["items"][0]
    assert (row["cached_names"], row["workspaces"], row["scopes"]) == (1, 3, 5)
