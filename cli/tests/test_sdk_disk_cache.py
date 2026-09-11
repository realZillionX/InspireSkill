"""Real process boundaries with fake catalog APIs and forbidden HTTP."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import sqlite3
from pathlib import Path
import subprocess
import sys

import pytest

from inspire.sdk.cache import CatalogCache
from inspire.sdk.catalog_store import CatalogStore, MAX_BYTES, MAX_ENTRIES, key_string
from inspire.sdk.exceptions import ResolutionIncompleteError, ValidationError

WORKER = r"""
import json, sys, time
from pathlib import Path
from inspire.config import Config
sys.path.insert(0, str(Path.cwd() / "tests"))
import pytest
from test_sdk_cache import catalog
from inspire.sdk import InspireClient, NotebookRef
from inspire.platform.web import browser_api as api
from inspire.platform.web.session.models import WebSession

home, account, origin, ttl, disk = sys.argv[1:]
m = pytest.MonkeyPatch()
m.setattr("inspire.local_files._restrict_windows_directory", lambda path: None)
m.setattr(Path, "home", lambda: Path(home))
def reject(*a, **kw):
    raise AssertionError("Real platform access is forbidden")
m.setattr("requests.sessions.Session.send", reject)
m.setattr("inspire.accounts.account_exists", lambda name: True)
m.setattr("inspire.config.Config.from_files_and_env", lambda **kw: (
    Config(base_url=origin, username="fake", password="unused"), None))
c = InspireClient(account, catalog_ttl=float(ttl), catalog_disk_cache=(disk == "true"))
c._transport._session = WebSession(
    storage_state={"cookies": []}, created_at=time.time(),
    workspace_id="ws-test", account=account, login_username="fake", base_url=origin)
c._transport._last_success = time.monotonic()
fake = catalog.__wrapped__(c, m)
for line in sys.stdin:
    action = json.loads(line)
    fake.calls.clear()
    racing = isinstance(action, str) and action.startswith("race_")
    if racing:
        from test_sdk_disk_cache import worker
        action = action.removeprefix("race_")
        method = {"register": "create_image", "delete": "delete_image",
                  "set_visibility": "update_image", "save_image": "save_notebook_as_image"}[action]
        def during_write(**kw):
            worker(Path(home), ["images"], account=account, origin=origin)
            return {"image_id": "new"}
        m.setattr(api, method, during_write)
    if action == "plan":
        c.jobs.plan(fake.spec)
    elif action == "images":
        c.images.get("Image:v1", workspace="Workspace")
    elif action in ("register", "delete", "set_visibility", "save_image"):
        image = c.images.get("Image:v1", workspace="Workspace")
        if action == "register":
            c.images.register("New", workspace="Workspace")
        elif action == "save_image":
            m.setattr(api, "estimate_notebook_image_size", lambda **kw: None)
            if not racing:
                m.setattr(api, "save_notebook_as_image", lambda **kw: {"image_id": "new"})
            c.notebooks.save_image(NotebookRef("nb", account, origin, "nb", "ws-test"), name="New")
        else:
            getattr(c.images, action)(image.ref, **(
                {"visibility": "public"} if action == "set_visibility" else {}))
    elif action == "clear":
        c.cache.clear()
    elif action == "live":
        m.setattr(api, "list_notebooks", fake.counted("ListNotebooks", ([], 0)))
        c.notebooks.list("Workspace")
    elif isinstance(action, dict):
        key = ("current_user", account, origin, action["scope"])
        c.cache._get(key, lambda: {"id": action["scope"]})
    print(json.dumps({"calls": fake.calls, "stats": c.cache.stats()}), flush=True)
c.close()
"""


def worker(
    tmp_path, actions, *, account="alpha", origin="https://example.invalid", ttl=60, disk=True
):
    result = subprocess.run(
        [sys.executable, "-c", WORKER, str(tmp_path), account, origin, str(ttl), str(disk).lower()],
        input="".join(json.dumps(a) + "\n" for a in actions),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return [json.loads(line) for line in result.stdout.splitlines()]


def store(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return CatalogStore("alpha", "https://example.invalid")


KEY = ("current_user", "alpha", "https://example.invalid")


def test_plan_reused_by_independent_process(tmp_path):
    first = worker(tmp_path, ["plan"])[0]
    second, third = worker(tmp_path, ["plan", "plan"])
    assert sum(first["calls"].values()) == 9
    assert second["calls"] == third["calls"] == {}
    assert second["stats"]["shared_hits"] == 10  # Includes the flag derived from routes.
    assert third["stats"]["hits"] > second["stats"]["hits"]
    assert third["stats"]["shared_hits"] == second["stats"]["shared_hits"]


@pytest.mark.parametrize("action", ["register", "delete", "set_visibility", "save_image", "clear"])
def test_mutation_invalidates_disk_and_a_running_readers_memory(tmp_path, action):
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            WORKER,
            str(tmp_path),
            "alpha",
            "https://example.invalid",
            "60",
            "true",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write('"images"\n')
        process.stdin.flush()
        assert json.loads(process.stdout.readline())["calls"]["ListImages"] == 4
        worker(tmp_path, [action])
        # A new process and the already-warm process both observe invalidation.
        fresh = worker(tmp_path, ["images"])[0]
        assert fresh["calls"]["ListImages"] == 4
        process.stdin.write('"images"\n')
        process.stdin.flush()
        warm = json.loads(process.stdout.readline())
        assert warm["stats"]["shared_hits"] >= 4
        assert warm["calls"] == {}
        process.stdin.close()
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


@pytest.mark.parametrize(
    "options",
    [
        {"account": "beta"},
        {"origin": "https://other.invalid"},
    ],
)
def test_accounts_and_servers_are_isolated(tmp_path, options):
    worker(tmp_path, ["plan"])
    assert sum(worker(tmp_path, ["plan"], **options)[0]["calls"].values()) == 9
    assert worker(tmp_path, ["plan"])[0]["calls"] == {}


@pytest.mark.parametrize("garbage", [b'{"version":', b"garbage", b"[]", b"\xff", b"null"])
def test_corrupt_store_is_a_miss(tmp_path, monkeypatch, garbage):
    disk = store(tmp_path, monkeypatch)
    worker(tmp_path, ["plan"])
    disk.path.write_bytes(garbage)
    assert worker(tmp_path, ["plan"])[0]["calls"] == {"GetScheduleConfig": 1}
    assert worker(tmp_path, ["plan"])[0]["calls"] == {}


def test_expiry_across_processes_and_cleanup(tmp_path, monkeypatch):
    disk = store(tmp_path, monkeypatch)
    worker(tmp_path, ["plan"])
    data = json.loads(disk.path.read_text(encoding="utf-8"))
    for entry in data["entries"].values():
        entry["created"] -= 120
        entry["expires"] -= 120
    disk.path.write_text(json.dumps(data))
    with sqlite3.connect(disk.path.parent / "resource-index.sqlite3") as connection:
        connection.execute("UPDATE resource_scope SET last_full_refresh_at = last_full_refresh_at - 120")
        connection.execute("UPDATE resource_identity SET observed_at = observed_at - 120, expires_at = expires_at - 120")
    assert worker(tmp_path, ["images"])[0]["calls"] == {"GetRoutes": 1, "ListImages": 4}
    # Reading metadata performs the remainder's expiry pruning.
    disk.read(KEY)
    assert len(json.loads(disk.path.read_text(encoding="utf-8"))["entries"]) == 0


def test_reader_ttl_caps_sdk_reads_without_shortening_cli_identity_ttl(tmp_path):
    worker(tmp_path, ["plan"], ttl=0.001)
    # The metadata writer TTL still expires; identity lifetimes are the CLI's.
    assert worker(tmp_path, ["plan"], ttl=600)[0]["calls"] == {"GetScheduleConfig": 1}
    assert sum(worker(tmp_path, ["plan"], ttl=0.001)[0]["calls"].values()) >= 9


def test_concurrent_process_writers_keep_all_entries(tmp_path, monkeypatch):
    disk = store(tmp_path, monkeypatch)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: worker(tmp_path, [{"scope": str(i)}]), range(16)))
    data = json.loads(disk.path.read_text(encoding="utf-8"))
    assert len(data["entries"]) == 16
    for i in range(16):
        entry = data["entries"][key_string((*KEY, str(i)))]
        assert disk.value(entry) == {"id": str(i)}
    # Windows does not implement POSIX owner-only permission bits.
    if os.name == "posix":
        assert disk.path.stat().st_mode & 0o777 == 0o600
    assert not disk.path.with_name(disk.path.name + ".tmp").exists()


def test_incomplete_enumeration_never_published(tmp_path, monkeypatch):
    disk = store(tmp_path, monkeypatch)
    cache = CatalogCache(store=disk)

    def fail():
        raise ResolutionIncompleteError("missing page")

    with pytest.raises(ResolutionIncompleteError):
        cache._get(KEY, fail)
    assert cache.stats()["entries"] == 0
    assert disk.read(KEY)[1] is None
    assert cache._get(KEY, lambda: {"id": "complete"}) == {"id": "complete"}


def test_inflight_enumeration_cannot_undo_process_invalidation(tmp_path, monkeypatch):
    disk = store(tmp_path, monkeypatch)
    cache = CatalogCache(store=disk)

    def load():
        worker(tmp_path, ["clear"])
        return {"id": "stale"}

    cache._get(KEY, load)
    assert cache.stats()["entries"] == 0
    assert disk.read(KEY)[1] is None


def test_store_and_memory_are_bounded(tmp_path, monkeypatch):
    disk = store(tmp_path, monkeypatch)
    cache = CatalogCache(store=disk)
    for i in range(MAX_ENTRIES + 3):
        cache._get((*KEY, str(i)), lambda: {"id": "user"})
    assert len(json.loads(disk.path.read_text(encoding="utf-8"))["entries"]) == MAX_ENTRIES
    assert cache.stats()["entries"] == MAX_ENTRIES
    cache._get(KEY, lambda: {"id": "large", "name": "x" * MAX_BYTES})
    assert disk.path.stat().st_size <= MAX_BYTES
    assert disk.read(KEY)[1] is None


def test_unknown_types_are_not_deserialized(tmp_path, monkeypatch):
    disk = store(tmp_path, monkeypatch)
    cache = CatalogCache(store=disk)
    cache._get(KEY, lambda: {"id": "user"})
    data = json.loads(disk.path.read_text(encoding="utf-8"))
    data["entries"][key_string(KEY)]["value"] = ["os.system", "false"]
    disk.path.write_text(json.dumps(data))
    assert CatalogCache(store=disk)._get(KEY, lambda: {"id": "fresh"}) == {"id": "fresh"}


def test_live_resources_are_not_shared(tmp_path):
    assert worker(tmp_path, ["live"])[0]["calls"]["ListNotebooks"] == 1
    assert worker(tmp_path, ["live"])[0]["calls"] == {"ListNotebooks": 1}


def test_ttl_zero_creates_no_store(tmp_path):
    result = worker(tmp_path, ["plan"], ttl=0)[0]
    assert result["stats"]["entries"] == 0
    assert not list(tmp_path.rglob("sdk-catalog*"))


def test_shared_cache_rejects_wrong_account_key(tmp_path, monkeypatch):
    cache = CatalogCache(store=store(tmp_path, monkeypatch))
    with pytest.raises(ValidationError):
        cache._get(("current_user", "beta", KEY[2]), lambda: {"id": "user"})


def test_opted_out_writer_invalidates_existing_shared_store(tmp_path):
    worker(tmp_path, ["images"])
    worker(tmp_path, ["register"], disk=False)
    assert worker(tmp_path, ["images"])[0]["calls"] == {"ListImages": 4}


@pytest.mark.parametrize("value", [None, 1, "yes"])
def test_disk_option_requires_boolean(value):
    from inspire.sdk import InspireClient

    with pytest.raises(ValidationError, match="catalog_disk_cache"):
        InspireClient(catalog_disk_cache=value)


def test_scope_parameters_and_integer_keys_round_trip(tmp_path, monkeypatch):
    disk = store(tmp_path, monkeypatch)
    cache = CatalogCache(store=disk)
    keys = [
        ("current_user", *KEY[1:], "ws", "group1", "train"),
        ("current_user", *KEY[1:], "ws", "group2", "train"),
        ("current_user", *KEY[1:], "ws", "group1", "notebook"),
        ("current_user", *KEY[1:], "other", "group1", "train"),
    ]
    for i, key in enumerate(keys):
        cache._get(key, lambda i=i: {"id": str(i)})
    other = CatalogCache(store=disk)

    def fail():
        pytest.fail("Complete catalog should come from disk")

    for i, key in enumerate(keys):
        assert other._get(key, fail) == {"id": str(i)}
    levels = ("priority_levels", *KEY[1:], "ws", "train")
    cache._get(levels, lambda: {"quota": {1: "low", 9: "high"}})
    assert other._get(levels, fail) == {"quota": {1: "low", 9: "high"}}


def test_invalid_row_is_a_miss(tmp_path, monkeypatch):
    disk = store(tmp_path, monkeypatch)
    worker(tmp_path, ["images"])
    with sqlite3.connect(disk.path.parent / "resource-index.sqlite3") as connection:
        connection.execute("UPDATE resource_identity SET payload = ? WHERE resource_type = 'workspace'", ('["dict", []]',))
    assert worker(tmp_path, ["images"])[0]["calls"] == {"GetRoutes": 1}


def test_unavailable_disk_does_not_serve_unchecked_memory(tmp_path, monkeypatch):
    disk = store(tmp_path, monkeypatch)
    cache = CatalogCache(store=disk)
    cache._get(KEY, lambda: {"id": "old"})

    def unavailable(key):
        raise OSError("unavailable")

    monkeypatch.setattr(disk, "read", unavailable)
    assert cache._get(KEY, lambda: {"id": "new"}) == {"id": "new"}
    assert cache.stats()["entries"] == 0


def test_invalidation_failure_is_not_silently_successful(tmp_path, monkeypatch):
    disk = store(tmp_path, monkeypatch)
    cache = CatalogCache(store=disk)

    def unavailable(prefix):
        raise OSError("unavailable")

    monkeypatch.setattr(disk, "invalidate", unavailable)
    with pytest.raises(OSError, match="unavailable"):
        cache._invalidate("images", *KEY[1:])


def test_shared_index_repaired_and_other_cli_disk_files_untouched(tmp_path):
    directory = tmp_path / ".inspire" / "accounts" / "alpha"
    directory.mkdir(parents=True)
    sentinels = {
        name: (directory / name)
        for name in (
            "resource-index.sqlite3",
            "notebook-targets.json",
        )
    }
    for path in sentinels.values():
        path.write_bytes(b"CLI-owned sentinel")
    worker(tmp_path, ["plan", "register", "clear"])
    assert sentinels["notebook-targets.json"].read_bytes() == b"CLI-owned sentinel"
    with sqlite3.connect(sentinels["resource-index.sqlite3"]) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM resource_identity").fetchone()[0] == 0


def test_default_reader_creates_no_shared_files(tmp_path):
    worker(tmp_path, ["plan", "register"], disk=False)
    assert not list(tmp_path.rglob("sdk-catalog*"))


@pytest.mark.parametrize("action", ["register", "delete", "set_visibility", "save_image"])
def test_reader_filling_during_write_is_invalidated_when_write_finishes(tmp_path, action):
    worker(tmp_path, ["images"])
    worker(tmp_path, ["race_" + action])
    assert worker(tmp_path, ["images"])[0]["calls"] == {"ListImages": 4}
