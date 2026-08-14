from __future__ import annotations

import json
import os
import sqlite3
import time
from importlib import import_module

import pytest
from click.testing import CliRunner

from inspire.accounts import create_account, set_current_account
from inspire.cli.context import EXIT_API_ERROR
from inspire.cli.utils.resource_index import (
    DEFAULT_TTL_SECONDS,
    ResourceIdentity,
    ResourceIndex,
    ResourceScope,
)
from inspire.cli.utils.resource_index_refresh import (
    RESOURCE_FETCHERS,
    FetchResult,
    RefreshResult,
    RefreshSummary,
    maybe_spawn_periodic_refresh,
    periodic_refresh_stamp_path,
    refresh_resource_index,
)
from inspire.platform.web.session.models import WebSession


class _Session:
    base_url = "https://inspire.example"
    login_username = "alice"
    user_detail = {"id": "user-one"}
    all_workspace_names = {"workspace-one": "Training Space"}


def _scope(resource_type: str, workspace_id: str = "") -> ResourceScope:
    return ResourceScope(
        base_url="https://inspire.example",
        subject_id="user-one",
        resource_type=resource_type,
        workspace_id=workspace_id,
        owner_scope=(
            "self"
            if resource_type not in {"workspace", "project", "compute-group"}
            else ""
        ),
    )


def _workspace_fetch(_session: object, _workspace: str, exact_name: str) -> FetchResult:
    records = [ResourceIdentity(resource_id="workspace-one", name="Training Space")]
    if exact_name:
        records = [record for record in records if record.name == exact_name]
    return FetchResult(records)


def _outcome_count(summary: RefreshSummary, outcome: str) -> int:
    return sum(result.outcome == outcome for result in summary.results)


def test_full_refresh_populates_scoped_name_map(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    calls: list[str] = []

    def _job_fetch(_session: object, workspace_id: str, exact_name: str) -> FetchResult:
        calls.append(workspace_id)
        records = [
            ResourceIdentity(
                resource_id="job-one",
                name="train",
                status="RUNNING",
            )
        ]
        return FetchResult(
            [record for record in records if not exact_name or record.name == exact_name]
        )

    summary = refresh_resource_index(
        session=_Session(),
        index=index,
        resource_types=("workspace", "job"),
        force=True,
        fetchers={"workspace": _workspace_fetch, "job": _job_fetch},
    )

    assert summary.error_count == 0
    assert _outcome_count(summary, "refreshed") == 2
    assert calls == ["workspace-one"]
    assert [
        item.resource_id for item in index.lookup(_scope("workspace"), "Training Space")
    ] == ["workspace-one"]
    assert [
        item.resource_id
        for item in index.lookup(_scope("job", "workspace-one"), "train")
    ] == ["job-one"]


def test_due_refresh_skips_fresh_scope_without_calling_fetcher(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    workspace_calls: list[str] = []
    index.reconcile(
        _scope("job", "workspace-one"),
        [ResourceIdentity(resource_id="job-one", name="train")],
    )

    def _unexpected_fetch(
        _session: object,
        _workspace_id: str,
        _exact_name: str,
    ) -> FetchResult:
        raise AssertionError("fresh scope should not call its resource fetcher")

    summary = refresh_resource_index(
        session=_Session(),
        index=index,
        resource_types=("job",),
        force=False,
        fetchers={
            "workspace": lambda *_args: (
                workspace_calls.append("workspace") or _workspace_fetch(*_args)
            ),
            "job": _unexpected_fetch,
        },
    )

    assert _outcome_count(summary, "fresh") == 1
    assert summary.error_count == 0
    assert workspace_calls == []


def test_exact_refresh_replaces_recreated_resource(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope("notebook", "workspace-one")
    index.upsert(
        scope,
        [ResourceIdentity(resource_id="notebook-old", name="A")],
    )

    summary = refresh_resource_index(
        session=_Session(),
        index=index,
        resource_types=("notebook",),
        workspace_names=("Training Space",),
        exact_name="A",
        force=True,
        fetchers={
            "workspace": _workspace_fetch,
            "notebook": lambda _session, _workspace, _name: FetchResult(
                [ResourceIdentity(resource_id="notebook-new", name="A")]
            ),
        },
    )

    assert summary.error_count == 0
    assert [
        item.resource_id
        for item in index.lookup(scope, "A", fresh_only=False, now=112)
    ] == [
        "notebook-new"
    ]
    old = index.lookup_id(scope, "notebook-old", include_tombstoned=True)
    assert old is not None
    assert old.tombstoned_at is not None


def test_refresh_does_not_overwrite_newer_write_through(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope("notebook", "workspace-one")
    index.upsert(
        scope,
        [ResourceIdentity(resource_id="notebook-old", name="A")],
    )

    def _notebook_fetch(_session: object, _workspace: str, _name: str) -> FetchResult:
        index.mark_deleted(scope, resource_id="notebook-old", now=110)
        index.upsert(
            scope,
            [ResourceIdentity(resource_id="notebook-new", name="A")],
            now=111,
        )
        return FetchResult(
            [ResourceIdentity(resource_id="notebook-old", name="A")]
        )

    summary = refresh_resource_index(
        session=_Session(),
        index=index,
        resource_types=("notebook",),
        workspace_names=("Training Space",),
        exact_name="A",
        force=True,
        fetchers={
            "workspace": _workspace_fetch,
            "notebook": _notebook_fetch,
        },
    )

    assert _outcome_count(summary, "stale") == 1
    assert summary.error_count == 0
    assert [
        item.resource_id
        for item in index.lookup(scope, "A", fresh_only=False, now=112)
    ] == ["notebook-new"]


def test_refresh_does_not_repopulate_cache_after_clear(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope("notebook", "workspace-one")

    def _notebook_fetch(_session: object, _workspace: str, _name: str) -> FetchResult:
        index.clear()
        return FetchResult(
            [ResourceIdentity(resource_id="notebook-old", name="A")]
        )

    summary = refresh_resource_index(
        session=_Session(),
        index=index,
        resource_types=("notebook",),
        workspace_names=("Training Space",),
        exact_name="A",
        force=True,
        fetchers={
            "workspace": _workspace_fetch,
            "notebook": _notebook_fetch,
        },
    )

    assert _outcome_count(summary, "stale") == 1
    assert summary.error_count == 0
    assert index.list_identities(scope, fresh_only=False) == []


def test_failed_refresh_preserves_existing_rows(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope("job", "workspace-one")
    index.reconcile(scope, [ResourceIdentity(resource_id="job-one", name="train")])

    def _fail(_session: object, _workspace: str, _name: str) -> FetchResult:
        raise RuntimeError("temporary API failure")

    summary = refresh_resource_index(
        session=_Session(),
        index=index,
        resource_types=("job",),
        force=True,
        fetchers={"workspace": _workspace_fetch, "job": _fail},
    )

    assert summary.error_count == 1
    assert [
        item.resource_id for item in index.lookup(scope, "train", fresh_only=False)
    ] == ["job-one"]
    assert index.list_scope_status()[0].last_error == "temporary API failure"


def test_refresh_continues_when_error_recording_fails(tmp_path, monkeypatch) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")

    def _fail(_session: object, _workspace: str, _name: str) -> FetchResult:
        raise RuntimeError("temporary API failure")

    monkeypatch.setattr(
        index,
        "record_refresh_error",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database unavailable")
        ),
    )
    summary = refresh_resource_index(
        session=_Session(),
        index=index,
        resource_types=("job",),
        force=True,
        fetchers={"workspace": _workspace_fetch, "job": _fail},
    )

    assert summary.error_count == 1
    assert summary.results[0].error == "temporary API failure"


def test_refresh_continues_when_tombstone_purge_fails(tmp_path, monkeypatch) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")

    monkeypatch.setattr(
        index,
        "purge_tombstones",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("database unavailable")
        ),
    )
    summary = refresh_resource_index(
        session=_Session(),
        index=index,
        resource_types=("workspace",),
        force=True,
        fetchers={"workspace": _workspace_fetch},
    )

    assert summary.error_count == 0
    assert _outcome_count(summary, "refreshed") == 1


def test_refresh_reports_database_error_when_lease_acquisition_fails(
    tmp_path,
    monkeypatch,
) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")

    def _broken_connect():
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(index, "_connect", _broken_connect)
    summary = refresh_resource_index(
        session=_Session(),
        index=index,
        resource_types=("job",),
        force=True,
        fetchers={
            "workspace": _workspace_fetch,
            "job": lambda *_args: (_ for _ in ()).throw(
                AssertionError("cache failure should not call the fetcher")
            ),
        },
    )

    assert summary.error_count == 1
    assert _outcome_count(summary, "busy") == 0
    assert summary.results[0].error == "The local resource name cache is unavailable."


def test_cache_status_is_stale_at_the_ttl_boundary(tmp_path, monkeypatch) -> None:
    cache_commands = import_module("inspire.cli.commands.cache")
    index = ResourceIndex(tmp_path / "index.sqlite3")
    index.reconcile(
        _scope("job", "workspace-one"),
        [ResourceIdentity(resource_id="job-one", name="train")],
        now=100,
    )

    ttl = DEFAULT_TTL_SECONDS["job"]

    monkeypatch.setattr(cache_commands.time, "time", lambda: 100 + ttl - 1)
    ready = cache_commands._status_payload(index)
    assert ready["items"][0]["state"] == "ready"

    monkeypatch.setattr(cache_commands.time, "time", lambda: 100 + ttl)
    stale = cache_commands._status_payload(index)
    assert stale["items"][0]["state"] == "stale"
    assert stale["items"][0]["cached_names"] == 0


def test_cache_status_marks_targeted_only_scope_partial(tmp_path, monkeypatch) -> None:
    cache_commands = import_module("inspire.cli.commands.cache")
    index = ResourceIndex(tmp_path / "index.sqlite3")
    index.replace_name(
        _scope("job", "workspace-one"),
        "train",
        [ResourceIdentity(resource_id="job-one", name="train")],
        now=100,
    )

    monkeypatch.setattr(cache_commands.time, "time", lambda: 101)
    payload = cache_commands._status_payload(index)

    assert payload["items"][0]["state"] == "partial"


def test_cache_status_stays_ready_after_targeted_refresh_of_full_scope(
    tmp_path,
    monkeypatch,
) -> None:
    cache_commands = import_module("inspire.cli.commands.cache")
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope("job", "workspace-one")
    index.reconcile(
        scope,
        [ResourceIdentity(resource_id="job-one", name="train")],
        now=100,
    )
    index.replace_name(
        scope,
        "train",
        [ResourceIdentity(resource_id="job-one", name="train")],
        now=110,
    )

    monkeypatch.setattr(cache_commands.time, "time", lambda: 111)
    payload = cache_commands._status_payload(index)

    assert payload["items"][0]["state"] == "ready"


def test_incomplete_workspace_snapshot_never_tombstones_unseen_rows(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope("workspace")
    index.reconcile(
        scope,
        [
            ResourceIdentity(resource_id="workspace-one", name="One"),
            ResourceIdentity(resource_id="workspace-two", name="Two"),
        ],
    )

    summary = refresh_resource_index(
        session=_Session(),
        index=index,
        resource_types=("workspace",),
        force=True,
        fetchers={
            "workspace": lambda _session, _workspace, _name: FetchResult(
                [ResourceIdentity(resource_id="workspace-one", name="One")],
                complete=False,
            )
        },
    )

    assert summary.error_count == 0
    assert [
        item.resource_id
        for item in index.lookup(scope, "Two", fresh_only=False)
    ] == ["workspace-two"]


def test_complete_workspace_snapshot_does_not_merge_session_rows(
    tmp_path,
    monkeypatch,
) -> None:
    from inspire.cli.utils import resource_index_refresh as refresh_module
    from inspire.platform.web.browser_api import workspaces

    index = ResourceIndex(tmp_path / "index.sqlite3")
    calls: list[str] = []
    session = _Session()
    session.all_workspace_names = {
        "workspace-live": "Live",
        "workspace-old": "Old",
    }

    monkeypatch.setattr(
        workspaces,
        "try_enumerate_workspaces",
        lambda _session: [{"id": "workspace-live", "name": "Live"}],
    )

    def _job_fetch(_session: object, workspace_id: str, _name: str) -> FetchResult:
        calls.append(workspace_id)
        return FetchResult([])

    summary = refresh_resource_index(
        session=session,
        index=index,
        resource_types=("workspace", "job"),
        force=True,
        fetchers={
            "workspace": refresh_module._workspace_fetch,
            "job": _job_fetch,
        },
    )

    assert summary.error_count == 0
    assert calls == ["workspace-live"]


def test_workspace_failure_preserves_last_successful_snapshot(
    tmp_path,
    monkeypatch,
) -> None:
    from inspire.cli.utils import resource_index_refresh as refresh_module
    from inspire.platform.web.browser_api import workspaces

    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope("workspace")
    index.reconcile(
        scope,
        [ResourceIdentity(resource_id="workspace-old", name="Old")],
    )

    def _fail(_session: object) -> list[dict]:
        raise RuntimeError("temporary workspace API failure")

    monkeypatch.setattr(workspaces, "try_enumerate_workspaces", _fail)

    summary = refresh_resource_index(
        session=_Session(),
        index=index,
        resource_types=("workspace",),
        force=True,
        fetchers={"workspace": refresh_module._workspace_fetch},
    )

    assert summary.error_count == 1
    assert [
        item.resource_id for item in index.lookup(scope, "Old", fresh_only=False)
    ] == ["workspace-old"]


def test_workspace_failure_is_preserved_for_workspace_bound_refresh(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")

    summary = refresh_resource_index(
        session=_Session(),
        index=index,
        resource_types=("job",),
        force=True,
        fetchers={
            "workspace": lambda *_args: (_ for _ in ()).throw(
                RuntimeError("workspace backend down")
            ),
            "job": lambda *_args: FetchResult([]),
        },
    )

    assert summary.error_count == 1
    assert summary.results[0].error == "workspace backend down"


def test_complete_workspace_refresh_prunes_removed_workspace_scopes(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    old_job_scope = _scope("job", "workspace-old")
    index.reconcile(
        _scope("workspace"),
        [ResourceIdentity(resource_id="workspace-old", name="Old")],
        now=100,
    )
    index.reconcile(
        old_job_scope,
        [ResourceIdentity(resource_id="job-old", name="train")],
        now=100,
    )

    summary = refresh_resource_index(
        session=_Session(),
        index=index,
        resource_types=("workspace", "job"),
        force=True,
        fetchers={
            "workspace": lambda *_args: FetchResult(
                [ResourceIdentity(resource_id="workspace-new", name="New")],
                complete=True,
            ),
            "job": lambda _session, workspace_id, _name: FetchResult(
                [ResourceIdentity(resource_id=f"job-{workspace_id}", name="train")]
            ),
        },
    )

    assert summary.error_count == 0
    assert index.lookup(old_job_scope, "train", fresh_only=False) == []
    assert "workspace-old" not in {
        status.workspace_id for status in index.list_scope_status()
    }


def test_empty_workspace_snapshot_is_distinct_from_failure(tmp_path) -> None:
    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope("workspace")
    index.reconcile(
        scope,
        [ResourceIdentity(resource_id="workspace-old", name="Old")],
    )

    summary = refresh_resource_index(
        session=_Session(),
        index=index,
        resource_types=("workspace",),
        force=True,
        fetchers={"workspace": lambda *_args: FetchResult([], complete=True)},
    )

    assert summary.error_count == 0
    assert index.lookup(scope, "Old", fresh_only=False) == []


def test_compute_group_failure_preserves_last_successful_rows(
    tmp_path,
    monkeypatch,
) -> None:
    from inspire.cli.utils import resource_index_refresh as refresh_module
    from inspire.platform.web.browser_api.availability import api

    index = ResourceIndex(tmp_path / "index.sqlite3")
    scope = _scope("compute-group", "workspace-one")
    index.reconcile(
        scope,
        [ResourceIdentity(resource_id="group-old", name="GPU")],
    )

    def _fail(*_args: object, **_kwargs: object) -> list[dict]:
        raise RuntimeError("temporary compute-group API failure")

    monkeypatch.setattr(api, "list_compute_groups", _fail)
    summary = refresh_resource_index(
        session=_Session(),
        index=index,
        resource_types=("compute-group",),
        force=True,
        fetchers={
            "workspace": _workspace_fetch,
            "compute-group": refresh_module._compute_group_fetch,
        },
    )

    assert summary.error_count == 1
    assert [
        item.resource_id for item in index.lookup(scope, "GPU", fresh_only=False)
    ] == ["group-old"]


def test_cache_status_and_clear_never_expose_workspace_handle(
    tmp_path,
    monkeypatch,
) -> None:
    from inspire.cli.main import main

    monkeypatch.setenv("HOME", str(tmp_path))
    create_account("alpha", "[inspire]\n")
    set_current_account("alpha")
    WebSession(
        storage_state={"cookies": [], "origins": []},
        created_at=time.time(),
        base_url="https://inspire.example",
        login_username="alice",
        user_detail={"id": "user-one"},
        all_workspace_names={"workspace-secret": "Training Space"},
    ).save()
    index = ResourceIndex.for_account()
    assert index is not None
    index.reconcile(
        ResourceScope(
            base_url="https://inspire.example",
            subject_id="user-one",
            resource_type="job",
            workspace_id="workspace-secret",
            owner_scope="self",
        ),
        [ResourceIdentity(resource_id="job-secret", name="train")],
    )

    runner = CliRunner()
    status = runner.invoke(main, ["--json", "cache", "status"])
    assert status.exit_code == 0
    assert "workspace-secret" not in status.output
    assert "job-secret" not in status.output
    payload = json.loads(status.output)["data"]
    by_resource = {item["resource"]: item for item in payload["items"]}
    resource = by_resource["job"]
    assert resource["cached_names"] == 1
    assert resource["state"] == "ready"
    assert resource["workspaces"] == 1
    assert str(resource["updated"]).endswith("s ago")
    assert by_resource["notebook-gpu"]["state"] == "empty"

    cleared = runner.invoke(main, ["cache", "clear", "--yes"])
    assert cleared.exit_code == 0
    assert cleared.output == "Cleared every local cache: 1 names, 0 GPU models.\n"
    assert index.list_scope_status() == []


def test_opening_the_index_drops_kinds_this_build_no_longer_knows(tmp_path) -> None:
    """`ssh-key` outlived its commands: unrefreshable, unclearable, still listed."""
    path = tmp_path / "index.sqlite3"
    index = ResourceIndex(path)
    index.reconcile(
        _scope("job", "workspace-one"),
        [ResourceIdentity(resource_id="job-1", name="train")],
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO resource_scope(
                base_url, subject_id, resource_type, workspace_id, owner_scope,
                last_refresh_at, last_full_refresh_at, last_error
            ) VALUES('https://inspire.example', 'user-one', 'ssh-key', '', 'self', 1, 1, '')
            """
        )
        connection.execute(
            """
            INSERT INTO resource_identity(
                base_url, subject_id, resource_type, workspace_id, owner_scope,
                resource_id, name, owner_id, status, created_at,
                observed_at, expires_at
            ) VALUES('https://inspire.example', 'user-one', 'ssh-key', '', 'self',
                     'key-1', 'laptop', '', '', '', 1, 9999999999)
            """
        )
        leftover = connection.execute(
            "SELECT COUNT(*) FROM resource_scope WHERE resource_type = 'ssh-key'"
        ).fetchone()[0]
    assert leftover == 1

    reopened = ResourceIndex(path)

    assert "ssh-key" not in {
        status.resource_type for status in reopened.list_scope_status()
    }
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM resource_identity WHERE resource_type = 'ssh-key'"
            ).fetchone()[0]
            == 0
        )
    # The kinds that still exist survive the sweep.
    assert len(reopened.lookup(_scope("job", "workspace-one"), "train")) == 1


def test_cache_status_reports_one_kind_at_a_time(tmp_path, monkeypatch) -> None:
    cache_commands = import_module("inspire.cli.commands.cache")
    index = ResourceIndex(tmp_path / "index.sqlite3")
    index.reconcile(
        _scope("job", "workspace-one"),
        [ResourceIdentity(resource_id="job-1", name="train")],
    )
    monkeypatch.setattr(cache_commands, "_workspace_name_map", lambda: {})

    everything = cache_commands._status_payload(index)
    assert [item["resource"] for item in everything["items"]] == ["job", "notebook-gpu"]

    only_job = cache_commands._status_payload(index, resources=["job"])
    assert [item["resource"] for item in only_job["items"]] == ["job"]
    assert only_job["items"][0]["cached_names"] == 1

    # A kind nothing has cached yet still answers for itself.
    uncached = cache_commands._status_payload(index, resources=["ray", "notebook-gpu"])
    assert uncached["items"] == [
        {
            "resource": "notebook-gpu",
            "cached_names": 0,
            "state": "empty",
            "updated": "never",
        },
        {"resource": "ray", "cached_names": 0, "state": "empty", "updated": "never"},
    ]


def test_cache_status_says_so_when_nothing_at_all_is_cached(tmp_path, monkeypatch) -> None:
    """The whole-cache view of nothing is a sentence, not a column of zeroes."""
    from inspire.cli.main import main

    monkeypatch.setenv("HOME", str(tmp_path))
    create_account("alpha", "[inspire]\n")
    set_current_account("alpha")
    WebSession(
        storage_state={"cookies": [], "origins": []},
        created_at=time.time(),
        base_url="https://inspire.example",
        login_username="alice",
        user_detail={"id": "user-one"},
    ).save()

    runner = CliRunner()
    everything = runner.invoke(main, ["cache", "status"])
    assert everything.exit_code == 0
    assert everything.output == "Resource name cache is empty.\n"

    one_kind = runner.invoke(main, ["cache", "status", "--resource", "notebook"])
    assert one_kind.exit_code == 0
    assert one_kind.output == "notebook: 0 names, empty, never\n"


def test_cache_clear_takes_one_kind_at_a_time(tmp_path, monkeypatch) -> None:
    """Dropping the notebook names must not cost the job names beside them."""
    from inspire.cli.commands.notebook import gpu_model as gpu_model_module
    from inspire.cli.main import main

    monkeypatch.setenv("HOME", str(tmp_path))
    create_account("alpha", "[inspire]\n")
    set_current_account("alpha")
    WebSession(
        storage_state={"cookies": [], "origins": []},
        created_at=time.time(),
        base_url="https://inspire.example",
        login_username="alice",
        user_detail={"id": "user-one"},
    ).save()
    index = ResourceIndex.for_account()
    assert index is not None
    index.reconcile(
        _scope("notebook", "workspace-one"),
        [ResourceIdentity(resource_id="nb-1", name="dev-box")],
    )
    index.reconcile(
        _scope("job", "workspace-one"),
        [ResourceIdentity(resource_id="job-1", name="train")],
    )
    monkeypatch.setattr(
        gpu_model_module.browser_api_module,
        "run_command_capture_in_notebook",
        lambda **_k: _GpuProbeResult(),
    )
    gpu_model_module.notebook_gpu_model(notebook_id="nb-1", compute_group="训练区")

    runner = CliRunner()
    cleared = runner.invoke(main, ["cache", "clear", "--resource", "notebook", "--yes"])

    assert cleared.exit_code == 0
    assert cleared.output == "Cleared notebook: 1 names.\n"
    assert index.lookup(_scope("notebook", "workspace-one"), "dev-box") == []
    assert len(index.lookup(_scope("job", "workspace-one"), "train")) == 1
    assert gpu_model_module.gpu_model_cache_status()[0] == 1

    cleared = runner.invoke(
        main, ["cache", "clear", "--resource", "notebook-gpu", "--yes"]
    )

    assert cleared.exit_code == 0
    assert cleared.output == "Cleared notebook-gpu: 1 GPU models.\n"
    assert gpu_model_module.gpu_model_cache_status()[0] == 0
    assert len(index.lookup(_scope("job", "workspace-one"), "train")) == 1


class _GpuProbeResult:
    returncode = 0
    output = "NVIDIA H200\n"
    completed = True


def test_cache_status_reports_name_only_refresh_failures(
    tmp_path,
    monkeypatch,
) -> None:
    cache_commands = import_module("inspire.cli.commands.cache")
    index = ResourceIndex(tmp_path / "index.sqlite3")
    index.record_refresh_error(
        _scope("job", "workspace-one"),
        f"request failed near {tmp_path / 'private.log'} for job-deadbeef",
        now=100,
    )
    monkeypatch.setattr(
        cache_commands,
        "_workspace_name_map",
        lambda: {"workspace-one": "Training Space"},
    )
    monkeypatch.setattr(cache_commands.time, "time", lambda: 101)

    assert cache_commands._status_payload(index) == {
        "items": [
            {
                "resource": "job",
                "cached_names": 0,
                "state": "error",
                "updated": "never",
                "workspaces": 1,
                "errors": 1,
                "failures": [
                    {
                        "workspace": "Training Space",
                        "error": "request failed near <redacted> for <redacted>",
                    }
                ],
            },
            {
                "resource": "notebook-gpu",
                "cached_names": 0,
                "state": "empty",
                "updated": "never",
            },
        ]
    }


def test_cache_refresh_json_failure_is_compact(tmp_path, monkeypatch) -> None:
    from inspire.cli.main import main

    cache_commands = import_module("inspire.cli.commands.cache")
    index = ResourceIndex(tmp_path / "index.sqlite3")
    monkeypatch.setattr(cache_commands, "_index_or_exit", lambda *_args: index)
    monkeypatch.setattr(
        cache_commands,
        "require_web_session",
        lambda *_args, **_kwargs: _Session(),
    )
    monkeypatch.setattr(
        cache_commands,
        "refresh_resource_index",
        lambda **_kwargs: RefreshSummary(
            [
                RefreshResult(
                    "job",
                    "Training Space",
                    0,
                    "error",
                    "API unavailable for job-deadbeef",
                )
            ]
        ),
    )

    result = CliRunner().invoke(main, ["--json", "cache", "refresh"])

    assert result.exit_code == EXIT_API_ERROR
    assert json.loads(result.output) == {
        "success": False,
        "data": {
            "refreshed": 0,
            "fresh": 0,
            "stale": 0,
            "busy": 0,
            "errors": 1,
            "names_cached": 0,
            "failures": [
                {
                    "workspace": "Training Space",
                    "resource": "job",
                    "error": "API unavailable for <redacted>",
                }
            ],
        },
    }


def test_cache_clear_json_requires_explicit_confirmation(
    tmp_path,
    monkeypatch,
) -> None:
    from inspire.cli.main import main

    cache_commands = import_module("inspire.cli.commands.cache")
    index = ResourceIndex(tmp_path / "index.sqlite3")
    monkeypatch.setattr(cache_commands, "_index_or_exit", lambda *_args: index)

    result = CliRunner().invoke(main, ["--json", "cache", "clear"])

    assert result.exit_code != 0
    assert json.loads(result.output) == {
        "success": False,
        "error": {
            "type": "ConfirmationRequired",
            "code": 12,
            "message": "Cache clearing requires confirmation.",
            "hint": "Pass --yes to confirm clearing the cache.",
        },
    }


def test_cache_refresh_workspace_metavar_accepts_name_or_all() -> None:
    from inspire.cli.main import main

    result = CliRunner().invoke(main, ["cache", "refresh", "--help"])

    assert result.exit_code == 0, result.output
    assert "--workspace NAME|all" in result.output
    assert "--workspace NAME " not in result.output
    assert "--workspace TEXT" not in result.output


@pytest.mark.parametrize(
    "command",
    [
        ["cache", "status"],
        ["cache", "clear", "--yes"],
        ["cache", "refresh"],
    ],
)
def test_cache_commands_normalize_database_errors(
    command,
    monkeypatch,
) -> None:
    from inspire.cli.main import main

    cache_commands = import_module("inspire.cli.commands.cache")

    def _fail_for_account(_cls, account=None):  # noqa: ANN001
        del account
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        cache_commands.ResourceIndex,
        "for_account",
        classmethod(_fail_for_account),
    )
    result = CliRunner().invoke(main, ["--json", *command])

    assert result.exit_code == EXIT_API_ERROR
    assert "CacheError" in result.output
    assert "database is locked" not in result.output


def test_quiet_refresh_preserves_failure_exit_code(tmp_path, monkeypatch) -> None:
    from inspire.cli.main import main

    cache_commands = import_module("inspire.cli.commands.cache")
    index = ResourceIndex(tmp_path / "index.sqlite3")
    monkeypatch.setattr(cache_commands, "_index_or_exit", lambda *_args: index)
    monkeypatch.setattr(cache_commands, "require_web_session", lambda *_args, **_kwargs: _Session())
    monkeypatch.setattr(
        cache_commands,
        "refresh_resource_index",
        lambda **_kwargs: RefreshSummary(
            [RefreshResult("job", "Training Space", 0, "error", "API unavailable")]
        ),
    )

    result = CliRunner().invoke(
        main,
        ["cache", "refresh", "--due", "--quiet"],
    )

    assert result.exit_code == EXIT_API_ERROR
    assert result.output == ""


def test_periodic_refresh_is_throttled_and_quiet(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    create_account("alpha", "[inspire]\n")
    set_current_account("alpha")
    WebSession(
        storage_state={"cookies": [], "origins": []},
        created_at=time.time(),
        base_url="https://inspire.example",
        login_username="alice",
        user_detail={"id": "user-one"},
        all_workspace_names={"workspace-one": "Training Space"},
    ).save()

    calls: list[dict[str, object]] = []

    class _Process:
        pass

    def _popen(command, **kwargs):  # noqa: ANN001
        calls.append({"command": command, **kwargs})
        return _Process()

    monkeypatch.setattr(
        "inspire.cli.utils.resource_index_refresh.subprocess.Popen",
        _popen,
    )

    assert maybe_spawn_periodic_refresh(interval_seconds=300) is True
    assert maybe_spawn_periodic_refresh(interval_seconds=300) is False
    assert calls[0]["command"][-4:] == [
        "cache",
        "refresh",
        "--due",
        "--quiet",
    ]
    assert calls[0]["env"]["INSPIRE_RESOURCE_INDEX_REFRESH_ACCOUNT"] == "alpha"
    stamp = periodic_refresh_stamp_path()
    assert stamp is not None
    assert stamp.exists()

    os.utime(stamp, (time.time() - 3600, time.time() - 3600))
    assert maybe_spawn_periodic_refresh(interval_seconds=7200) is True
    assert len(calls) == 2


def test_project_refresh_is_global_not_per_workspace(tmp_path) -> None:
    """A project spans workspaces, so it is fetched and cached once."""
    index = ResourceIndex(tmp_path / "index.sqlite3")
    fetch_workspaces: list[str] = []

    class _MultiWorkspaceSession(_Session):
        all_workspace_names = {
            "workspace-one": "Training Space",
            "workspace-two": "CPU Space",
        }

    def _multi_workspace_fetch(
        _session: object, _workspace: str, exact_name: str
    ) -> FetchResult:
        records = [
            ResourceIdentity(resource_id="workspace-one", name="Training Space"),
            ResourceIdentity(resource_id="workspace-two", name="CPU Space"),
        ]
        if exact_name:
            records = [record for record in records if record.name == exact_name]
        return FetchResult(records)

    def _project_fetch(_session: object, workspace_id: str, _name: str) -> FetchResult:
        fetch_workspaces.append(workspace_id)
        return FetchResult(
            [ResourceIdentity(resource_id="project-one", name="CI-情境智能")]
        )

    summary = refresh_resource_index(
        session=_MultiWorkspaceSession(),
        index=index,
        resource_types=("workspace", "project"),
        force=True,
        fetchers={"workspace": _multi_workspace_fetch, "project": _project_fetch},
    )

    assert summary.error_count == 0
    # One fetch total, with no workspace, rather than one per workspace.
    assert fetch_workspaces == [""]
    assert [
        item.resource_id for item in index.lookup(_scope("project"), "CI-情境智能")
    ] == ["project-one"]
    # Nothing landed under a workspace-scoped key.
    assert index.lookup(_scope("project", "workspace-one"), "CI-情境智能") == []


def test_project_lookup_ignores_the_caller_workspace(tmp_path, monkeypatch) -> None:
    """Callers still pass a workspace; the scope normalizer drops it."""
    from inspire.cli.context import Context
    from inspire.cli.utils.id_resolver import resolve_by_name

    index = ResourceIndex(tmp_path / "index.sqlite3")
    index.upsert(
        _scope("project"),
        [ResourceIdentity(resource_id="project-one", name="CI-情境智能")],
    )

    def _forbidden_lister():
        raise AssertionError("a cached project must not trigger a live list")

    for workspace_id in ("workspace-one", "workspace-two", ""):
        assert (
            resolve_by_name(
                Context(),
                name="CI-情境智能",
                resource_type="project",
                list_candidates=_forbidden_lister,
                session=_Session(),
                workspace_id=workspace_id,
                cache_index=index,
            )
            == "project-one"
        )


def test_quota_refresh_warms_one_workload_catalog(tmp_path, monkeypatch) -> None:
    """Quota is an ordinary resource type: `cache refresh --resource` reaches it."""
    from inspire.cli.utils import quota_cache as quota_cache_module

    index = ResourceIndex(tmp_path / "index.sqlite3")
    groups = [
        {"logic_compute_group_id": "lcg-a", "name": "训练区-H200-1号机房"},
        {"logic_compute_group_id": "lcg-b", "name": "CPU资源-2"},
    ]
    monkeypatch.setattr(
        quota_cache_module.browser_api_module,
        "list_notebook_compute_groups",
        lambda **_kwargs: groups,
    )
    # Notebook quota reads from ``GetScheduleConfig.quota`` (and back-fills
    # blank gpu_type from the group's nodes); stub the same boundary so the
    # refresh path stays hermetic. The v1 prices stub has no effect.
    monkeypatch.setattr(
        quota_cache_module.browser_api_module,
        "get_schedule_config_specs",
        lambda **kwargs: [
            {
                "id": "q-8",
                "cpu_count": 160,
                "memory_size": 1800,
                "gpu_count": 8,
                "gpu_type": "NVIDIA_H200",
                "logic_compute_group_ids": ["lcg-a"],
            }
        ],
    )
    monkeypatch.setattr(
        quota_cache_module.browser_api_module,
        "get_group_node_gpu_type",
        lambda *args, **_kwargs: "",
    )

    summary = refresh_resource_index(
        session=_Session(),
        index=index,
        resource_types=("quota-notebook",),
        force=True,
        fetchers={
            "workspace": _workspace_fetch,
            "quota-notebook": RESOURCE_FETCHERS["quota-notebook"],
        },
    )

    assert summary.error_count == 0
    scope = _scope("quota-notebook", "workspace-one")
    matches = index.lookup(scope, "8,160,1800")
    assert [item.resource_id for item in matches] == ["q-8"]
    assert matches[0].compute_group == "训练区-H200-1号机房"
    assert matches[0].owner_id == "lcg-a"


def test_cache_status_reports_quota_like_any_other_resource(tmp_path, monkeypatch) -> None:
    cache_commands = import_module("inspire.cli.commands.cache")
    index = ResourceIndex(tmp_path / "index.sqlite3")
    index.reconcile(
        _scope("quota-ray", "workspace-one"),
        [ResourceIdentity(resource_id="q-1", name="1,20,200", owner_id="lcg-a")],
        now=100,
    )
    ttl = DEFAULT_TTL_SECONDS["quota-ray"]

    monkeypatch.setattr(cache_commands.time, "time", lambda: 100 + ttl - 1)
    rows = {
        str(row["resource"]): row
        for row in cache_commands._status_payload(index)["items"]
    }
    assert rows["quota-ray"]["cached_names"] == 1
    assert rows["quota-ray"]["state"] == "ready"

    monkeypatch.setattr(cache_commands.time, "time", lambda: 100 + ttl)
    stale = {
        str(row["resource"]): row
        for row in cache_commands._status_payload(index)["items"]
    }
    assert stale["quota-ray"]["state"] == "stale"


def test_dedupe_preserves_every_identity_field() -> None:
    """Dedupe must not drop fields; it used to rebuild records by hand."""
    from dataclasses import fields

    from inspire.cli.utils.resource_index_refresh import _dedupe_records

    record = ResourceIdentity(
        resource_id="  q-8  ",
        name="  8,160,1800  ",
        owner_id="lcg-a",
        status="READY",
        created_at="2026-01-01",
        compute_group="训练区-H200-1号机房",
        payload='{"quota_id": "q-8"}',
    )

    deduped = _dedupe_records([record])[0]

    assert deduped.resource_id == "q-8"
    assert deduped.name == "8,160,1800"
    for field in fields(ResourceIdentity):
        if field.name in {"resource_id", "name"}:
            continue
        assert getattr(deduped, field.name) == getattr(record, field.name), field.name
