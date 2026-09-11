"""CLI/SDK shared writer and exact-resolution contracts; no platform access."""

from __future__ import annotations

from collections import Counter
import json
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from test_sdk import client as client
from test_sdk_cache import catalog as catalog
from test_sdk_disk_cache import worker
from inspire.sdk import AmbiguousResourceError, JobRef, ResourceNotFoundError
from inspire.sdk.identity_cache import IdentityCache
from inspire.services.catalog.resource_index import ResourceIdentity, ResourceIndex, ResourceScope


@pytest.mark.parametrize("state", ["present", "absent", "stale", "corrupt"])
@pytest.mark.parametrize("names", [["Target"], ["Target-extra"], ["Target", "TARGET"]])
def test_exact_resolution_is_independent_of_index_state(client, monkeypatch, tmp_path, state, names):
    path = tmp_path / "shared.sqlite3"
    monkeypatch.setattr("inspire.services.catalog.resource_index.resource_index_path", lambda account=None: path)
    session = client._transport._session
    scope = ResourceScope(client.base_url, "login:" + session.login_username, "job", "ws-test", "self")
    index = ResourceIndex(path)
    rows = [SimpleNamespace(name=name, ref=JobRef(name, client.account, client.base_url, f"j{i}", "ws-test")) for i, name in enumerate(names)]
    if state == "present":
        index.reconcile(scope, [ResourceIdentity(row.ref.key, row.name) for row in rows])
    elif state == "stale":
        index.reconcile(scope, [ResourceIdentity("wrong-old-id", "Target")], now=1)
    elif state == "corrupt":
        path.write_bytes(b"not a database")
    calls = Counter()

    def scan():
        calls["scan"] += 1
        return rows

    def detail(ref):
        calls["detail"] += 1
        return next(row for row in rows if row.ref.key == ref.key)

    monkeypatch.setattr(client.jobs, "get", detail)
    client.cache._identity = IdentityCache(client.account, 60, client.base_url)
    if names == ["Target"]:
        assert client.jobs._indexed_resolution("target", JobRef, "ws-test", scan).key == "j0"
        assert calls == ({"detail": 1} if state == "present" else {"scan": 1})
    elif names == ["Target-extra"]:
        with pytest.raises(ResourceNotFoundError):
            client.jobs._indexed_resolution("target", JobRef, "ws-test", scan)
        assert calls == {"scan": 1}
    else:
        with pytest.raises(AmbiguousResourceError) as error:
            client.jobs._indexed_resolution("target", JobRef, "ws-test", scan)
        assert {row.ref.key for row in error.value.candidates} == {"j0", "j1"}
        assert calls == {"scan": 1}
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_index_hit_with_renamed_live_handle_falls_back(client, monkeypatch, tmp_path):
    path = tmp_path / "index.sqlite3"
    monkeypatch.setattr("inspire.services.catalog.resource_index.resource_index_path", lambda account=None: path)
    session = client._transport._session
    scope = ResourceScope(client.base_url, "login:" + session.login_username, "job", "ws-test", "self")
    index = ResourceIndex(path)
    index.reconcile(scope, [ResourceIdentity("old", "Target")])
    client.cache._identity = IdentityCache(client.account, 60, client.base_url)
    replacement = SimpleNamespace(name="Target", ref=JobRef("Target", client.account, client.base_url, "new", "ws-test"))
    monkeypatch.setattr(client.jobs, "get", lambda ref: SimpleNamespace(name="Renamed", ref=ref))
    assert client.jobs._indexed_resolution("Target", JobRef, "ws-test", lambda: [replacement]).key == "new"
    assert index.lookup_id(scope, "old", include_tombstoned=True).tombstoned_at is not None


def test_unicode_casefold_ambiguity_is_not_sqlite_nocase(client, monkeypatch, tmp_path):
    path = tmp_path / "index.sqlite3"
    monkeypatch.setattr("inspire.services.catalog.resource_index.resource_index_path", lambda account=None: path)
    session = client._transport._session
    scope = ResourceScope(client.base_url, "login:" + session.login_username, "job", "ws-test", "self")
    index = ResourceIndex(path)
    rows = [SimpleNamespace(name=name, ref=JobRef(name, client.account, client.base_url, str(i), "ws-test")) for i, name in enumerate(["Straße", "STRASSE"])]
    index.reconcile(scope, [ResourceIdentity(row.ref.key, row.name) for row in rows])
    client.cache._identity = IdentityCache(client.account, 60, client.base_url)
    with pytest.raises(AmbiguousResourceError) as error:
        client.jobs._indexed_resolution("strasse", JobRef, "ws-test", lambda: rows)
    assert len(error.value.candidates) == 2


WRITER = r"""
import json, sys, time
from pathlib import Path
from types import SimpleNamespace
from inspire.services.catalog.resource_index import ResourceIndex, ResourceScope, ResourceIdentity
from inspire.services.catalog.resource_refresh import FetchResult
import requests
requests.sessions.Session.send = lambda *a, **k: (_ for _ in ()).throw(AssertionError("No HTTP"))
path, role, ready, release = map(Path, sys.argv[1:])
session = SimpleNamespace(base_url="https://example.invalid", login_username="fake", user_detail=None)
index = ResourceIndex(path)
lease = index.refresh_lease
index.refresh_lease = lambda scope, **kw: lease(scope, lease_seconds=1, **kw)
def load():
    ready.write_text("ready")
    while not release.exists():
        time.sleep(.01)
    return [{"id": str(role), "name": "Group"}]
if str(role) == "cli":
    from inspire.cli.utils.resource_index_refresh import _refresh_one
    result = _refresh_one(index=index, session=session, resource_type="compute-group",
        workspace_id="ws", workspace_name="", exact_name="", force=True,
        fetcher=lambda *a: FetchResult([ResourceIdentity(row["id"], row["name"]) for row in load()]))
    print(json.dumps({"outcome": result.outcome}), flush=True)
else:
    from inspire.sdk.identity_cache import IdentityCache
    import inspire.services.catalog.resource_index as module
    module.resource_index_path = lambda account=None: path
    value, shared = IdentityCache("alpha", 60, session.base_url).get(session, "compute_groups", ("ws",), load)
    print(json.dumps({"value": value, "shared": shared}), flush=True)
"""


@pytest.mark.parametrize("first", ["cli", "sdk"])
def test_real_cli_and_sdk_writers_honor_the_same_lease(tmp_path, first):
    path = tmp_path / "index.sqlite3"
    index = ResourceIndex(path)
    processes = []
    def start(role):
        process = subprocess.Popen([sys.executable, "-c", WRITER, str(path), role,
                                    str(tmp_path / (role + ".ready")), str(tmp_path / (role + ".release"))],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(process)
        return process
    def ready(role):
        deadline = time.monotonic() + 10
        while not (tmp_path / (role + ".ready")).exists():
            assert time.monotonic() < deadline, "writer failed to enter fetch"
            time.sleep(.01)
    second = "sdk" if first == "cli" else "cli"
    try:
        winner = start(first)
        ready(first)
        time.sleep(1.2)  # The owner must renew beyond the original one-second lease.
        loser = start(second)
        if second == "sdk":
            # The SDK may read live on contention, but cannot publish that read.
            ready(second)
            (tmp_path / (second + ".release")).touch()
        output, errors = loser.communicate(timeout=10)
        assert loser.returncode == 0, errors
        if second == "cli":
            assert json.loads(output)["outcome"] == "busy"
        else:
            assert json.loads(output)["shared"] is False
        scope = ResourceScope("https://example.invalid", "login:fake", "compute-group", "ws")
        assert index.list_identities(scope) == []
        (tmp_path / (first + ".release")).touch()
        _, errors = winner.communicate(timeout=10)
        assert winner.returncode == 0, errors
        assert [row.resource_id for row in index.list_identities(scope)] == [first]
        assert index.scope_revision(scope) == 1
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM refresh_lease").fetchone()[0] == 0
            assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


def test_sdk_catalog_rows_and_quota_are_not_json_blobs(tmp_path):
    first = worker(tmp_path, ["plan"])[0]
    second = worker(tmp_path, ["plan"])[0]
    assert sum(first["calls"].values()) == 9
    assert second["calls"] == {}
    directory = tmp_path / ".inspire/accounts/alpha"
    data = json.loads((directory / "sdk-catalog-v1.json").read_text())
    from inspire.services.catalog.catalog_codec import decode_catalog
    assert {decode_catalog(json.loads(key))[0] for key in data["entries"]} == {"fair_scheduling", "priority_levels"}
    with sqlite3.connect(directory / "resource-index.sqlite3") as connection:
        assert {row[0] for row in connection.execute("SELECT DISTINCT resource_type FROM resource_identity")} == {"workspace", "project", "compute-group", "image", "quota-job"}


def test_cli_and_sdk_share_module_objects_and_helpers():
    from inspire.services.catalog import resource_index, quota_cache
    from inspire.services.catalog import resource_index as shared_index, quota_cache as shared_quota
    assert resource_index is shared_index
    assert quota_cache is shared_quota


def test_name_resolution_platform_request_counts(client, monkeypatch, tmp_path):
    from dataclasses import make_dataclass
    from inspire.sdk import WorkspaceRef
    from inspire.platform.web.browser_api import jobs

    path = tmp_path / "requests.sqlite3"
    monkeypatch.setattr("inspire.services.catalog.resource_index.resource_index_path", lambda account=None: path)
    session = client._transport._session
    scope = ResourceScope(client.base_url, "login:" + session.login_username, "job", "ws-test", "self")
    index = ResourceIndex(path)
    Row = make_dataclass("Row", [("job_id", str), ("name", str)])
    rows = [Row(str(i), "Target" if i == 209 else f"Target-extra-{i}") for i in range(210)]
    calls = Counter()
    def page(**kwargs):
        calls["ListJobs"] += 1
        start = (kwargs["page_num"] - 1) * kwargs["page_size"]
        return rows[start:start + kwargs["page_size"]], len(rows)
    def detail(key, **kwargs):
        calls["GetJob"] += 1
        return {"job_id": key, "name": "Target", "status": "RUNNING"}
    monkeypatch.setattr(jobs, "list_jobs", page)
    monkeypatch.setattr(jobs, "get_job_detail_v2", detail)
    ws = WorkspaceRef("Workspace", client.account, client.base_url, "ws-test", "ws-test")
    observed = {}
    for mode in ("before_absent", "before_present", "after_absent", "after_present"):
        index.clear()
        if mode.endswith("present"):
            index.reconcile(scope, [ResourceIdentity(row.job_id, row.name) for row in rows])
        client.cache._identity = IdentityCache(client.account, 60, client.base_url) if mode.startswith("after") else None
        calls.clear()
        with client._transport.scope():
            assert client.jobs._resolve("Target", ws).key == "209"
        observed[mode] = dict(calls)
    assert observed == {
        "before_absent": {"ListJobs": 3}, "before_present": {"ListJobs": 3},
        "after_absent": {"ListJobs": 3}, "after_present": {"GetJob": 1},
    }


def test_quota_reader_uses_cli_scope_without_a_blob(client, catalog, monkeypatch, tmp_path):
    from inspire.services.catalog.quota_cache import quota_records
    path = tmp_path / "quota.sqlite3"
    monkeypatch.setattr("inspire.services.catalog.resource_index.resource_index_path", lambda account=None: path)
    session = client._transport._session
    scope = ResourceScope(client.base_url, "login:" + session.login_username, "quota-job", "ws-test", "self")
    index = ResourceIndex(path)
    index.reconcile(scope, quota_records(catalog.prices, logic_compute_group_id="g", compute_group_name="Group"))
    client.cache._identity = IdentityCache(client.account, 60, client.base_url)
    client.jobs.plan(catalog.spec)
    assert catalog.calls["GetLogicComputeGroupResourceSpecPrices"] == 0
    assert sum(catalog.calls.values()) == 8


def test_incomplete_sdk_catalog_keeps_cli_snapshot(client, monkeypatch, tmp_path):
    from inspire.sdk.exceptions import ResolutionIncompleteError
    path = tmp_path / "incomplete.sqlite3"
    monkeypatch.setattr("inspire.services.catalog.resource_index.resource_index_path", lambda account=None: path)
    session = client._transport._session
    scope = ResourceScope(client.base_url, "login:" + session.login_username, "compute-group", "ws-test")
    index = ResourceIndex(path)
    index.reconcile(scope, [ResourceIdentity("keep", "Group")], now=1)
    shared = IdentityCache(client.account, 60, client.base_url)
    def fail():
        raise ResolutionIncompleteError("missing page")
    with pytest.raises(ResolutionIncompleteError, match="missing page"):
        shared.get(session, "compute_groups", ("ws-test",), fail)
    assert [row.resource_id for row in index.list_identities(scope, fresh_only=False)] == ["keep"]
    assert index.list_scope_status()[0].last_error == "missing page"


def test_concurrent_openers_repair_corruption_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"not sqlite")
    code = """
import sys
from pathlib import Path
module = "inspire.services.catalog.resource_index" if int(sys.argv[2]) % 2 else "inspire.services.catalog.resource_index"
from importlib import import_module
m = import_module(module)
index = m.ResourceIndex(Path(sys.argv[1]))
index.upsert(m.ResourceScope("https://example.invalid", "user", "job", "ws", "self"),
             [m.ResourceIdentity(sys.argv[2], "row-" + sys.argv[2])])
"""
    def open_one(i):
        subprocess.run([sys.executable, "-c", code, str(path), str(i)], check=True, capture_output=True, timeout=15)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(open_one, range(8)))
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM resource_identity").fetchone()[0] == 8


def test_sdk_refresh_keeps_cli_ttl(client, catalog, monkeypatch, tmp_path):
    from inspire.services.catalog.resource_index import DEFAULT_TTL_SECONDS
    path = tmp_path / "ttl.sqlite3"
    monkeypatch.setattr("inspire.services.catalog.resource_index.resource_index_path", lambda account=None: path)
    client.cache._identity = IdentityCache(client.account, .001, client.base_url)
    client.jobs.plan(catalog.spec)
    with sqlite3.connect(path) as connection:
        for kind, observed, expires in connection.execute("SELECT resource_type, observed_at, expires_at FROM resource_identity"):
            assert expires - observed == DEFAULT_TTL_SECONDS[kind]


def test_multi_group_cold_plan_request_tradeoff(client, catalog):
    catalog.groups.extend([dict(catalog.groups[0], id=f"g{i}", name=f"Group {i}") for i in (2, 3)])
    client.jobs.plan(catalog.spec)
    assert sum(catalog.calls.values()) == 9
    assert catalog.calls["GetLogicComputeGroupResourceSpecPrices"] == 1
    client.cache.clear()
    catalog.calls.clear()
    client.cache._identity = IdentityCache(client.account, 60, client.base_url)
    client.jobs.plan(catalog.spec)
    assert sum(catalog.calls.values()) == 11
    assert catalog.calls["GetLogicComputeGroupResourceSpecPrices"] == 3
    catalog.calls.clear()
    client.jobs.plan(catalog.spec)
    assert catalog.calls == {}


def test_index_mutation_during_detail_cannot_hide_new_ambiguity(client, monkeypatch, tmp_path):
    path = tmp_path / "race.sqlite3"
    monkeypatch.setattr("inspire.services.catalog.resource_index.resource_index_path", lambda account=None: path)
    session = client._transport._session
    scope = ResourceScope(client.base_url, "login:" + session.login_username, "job", "ws-test", "self")
    index = ResourceIndex(path)
    index.reconcile(scope, [ResourceIdentity("one", "Target")])
    rows = [SimpleNamespace(name="Target", ref=JobRef("Target", client.account, client.base_url, key, "ws-test")) for key in ("one", "two")]
    def detail(ref):
        index.upsert(scope, [ResourceIdentity("two", "Target")])
        return rows[0]
    monkeypatch.setattr(client.jobs, "get", detail)
    client.cache._identity = IdentityCache(client.account, 60, client.base_url)
    with pytest.raises(AmbiguousResourceError) as error:
        client.jobs._indexed_resolution("Target", JobRef, "ws-test", lambda: rows)
    assert {row.ref.key for row in error.value.candidates} == {"one", "two"}


def test_shared_quota_scope_parameters_round_trip(client, monkeypatch, tmp_path):
    from inspire.platform.web import browser_api
    path = tmp_path / "scope-keys.sqlite3"
    monkeypatch.setattr("inspire.services.catalog.resource_index.resource_index_path", lambda account=None: path)
    session = client._transport._session
    calls = []
    def price(**kwargs):
        key = (kwargs["workspace_id"], kwargs["logic_compute_group_id"], kwargs["schedule_config_type"])
        calls.append(key)
        return [{"quota_id": ":".join(key), "gpu_count": 0, "cpu_count": 8, "memory_size_gib": 32}]
    monkeypatch.setattr(browser_api, "get_resource_prices", price)
    def groups():
        return [{"id": "g1", "name": "One"}, {"id": "g2", "name": "Two"}]
    keys = [
        ("ws", "g1", "SCHEDULE_CONFIG_TYPE_TRAIN"),
        ("ws", "g2", "SCHEDULE_CONFIG_TYPE_TRAIN"),
        ("ws", "g1", "SCHEDULE_CONFIG_TYPE_DSW"),
        ("other", "g1", "SCHEDULE_CONFIG_TYPE_TRAIN"),
    ]
    first = IdentityCache(client.account, 60, client.base_url)
    for key in keys:
        rows, _ = first.get(session, "prices", key, lambda: pytest.fail("unexpected fallback"), groups=groups)
        assert rows[0]["quota_id"] == ":".join(key)
    assert len(calls) == 6  # Three complete scopes, two groups in each.
    second = IdentityCache(client.account, 60, client.base_url)
    for key in keys:
        rows, shared = second.get(session, "prices", key, lambda: pytest.fail("unexpected fallback"), groups=groups)
        assert shared
        assert rows[0]["quota_id"] == ":".join(key)
    assert len(calls) == 6
