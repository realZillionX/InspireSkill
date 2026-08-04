from __future__ import annotations

import json
import os
import time
from importlib import import_module

from click.testing import CliRunner

from inspire.accounts import create_account, set_current_account
from inspire.cli.utils.resource_index import (
    ResourceIdentity,
    ResourceIndex,
    ResourceScope,
)
from inspire.cli.utils.resource_index_refresh import (
    FetchResult,
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
    assert summary.refreshed_count == 2
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
        fetchers={"workspace": _workspace_fetch, "job": _unexpected_fetch},
    )

    assert summary.skipped_count == 1
    assert summary.error_count == 0


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
    assert [item.resource_id for item in index.lookup(scope, "A")] == [
        "notebook-new"
    ]
    old = index.lookup_id(scope, "notebook-old", include_tombstoned=True)
    assert old is not None
    assert old.tombstoned_at is not None


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


def test_cache_status_is_stale_at_the_ttl_boundary(tmp_path, monkeypatch) -> None:
    cache_commands = import_module("inspire.cli.commands.cache")
    index = ResourceIndex(tmp_path / "index.sqlite3")
    index.reconcile(
        _scope("job", "workspace-one"),
        [ResourceIdentity(resource_id="job-one", name="train")],
        now=100,
    )

    monkeypatch.setattr(cache_commands.time, "time", lambda: 159)
    ready = cache_commands._status_payload(index)
    assert ready["resources"][0]["state"] == "ready"

    monkeypatch.setattr(cache_commands.time, "time", lambda: 160)
    stale = cache_commands._status_payload(index)
    assert stale["resources"][0]["state"] == "stale"


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
    assert len(payload["resources"]) == 1
    resource = payload["resources"][0]
    assert resource["resource"] == "job"
    assert resource["items"] == 1
    assert resource["state"] == "ready"
    assert resource["workspaces"] == 1
    assert str(resource["updated"]).endswith("s ago")

    cleared = runner.invoke(main, ["cache", "clear", "--yes"])
    assert cleared.exit_code == 0
    assert cleared.output == "Resource name cache cleared.\n"
    assert index.list_scope_status() == []


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
    stamp = periodic_refresh_stamp_path()
    assert stamp is not None
    assert stamp.exists()

    os.utime(stamp, (time.time() - 3600, time.time() - 3600))
    assert maybe_spawn_periodic_refresh(interval_seconds=300) is False
