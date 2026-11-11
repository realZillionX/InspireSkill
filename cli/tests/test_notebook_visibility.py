"""Local submission markers bound eventual-consistency retries."""

from types import SimpleNamespace
import sqlite3
import pytest
from inspire.cli.commands.notebook import notebook_lookup as lookup
from inspire.cli.context import Context
from inspire.platform.web.browser_api import notebooks as api
from inspire.services.catalog import resource_index as storage


@pytest.fixture
def clock(monkeypatch):
    now, sleeps = [1000.0], []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(lookup.time, "time", lambda: now[0])
    monkeypatch.setattr(lookup.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(lookup.time, "sleep", sleep)
    return now, sleeps


@pytest.fixture
def submission(monkeypatch):
    session = SimpleNamespace(account="alice", workspace_id="ws-test")
    kwargs = dict(
        name="fresh",
        workspace_id="ws-test",
        project_id="project",
        project_name="project",
        image_id="image",
        image_url="image",
        logic_compute_group_id="group",
        quota_id="quota",
        gpu_count=0,
        cpu_count=1,
        memory_size=1,
        shared_memory_size=1,
        auto_stop=False,
    )
    monkeypatch.setattr(api, "_notebook_v2", lambda *a, **kw: {"notebook_id": "nb-1"})
    monkeypatch.setattr(lookup, "_try_get_current_user_ids", lambda *a, **kw: ["user"])
    return session, kwargs


def resolve(session, require_live=True):
    return lookup._resolve_notebook_target(
        Context(),
        session=session,
        base_url="https://example.invalid",
        identifier="fresh",
        workspace_ids=["ws-test"],
        json_output=True,
        require_live=require_live,
    )


@pytest.mark.parametrize("require_live", [False, True])
@pytest.mark.parametrize("batch", [False, True])
def test_resolve_notebook_id_retries_until_eventual_consistency_settles(
    monkeypatch,
    clock,
    submission,
    require_live,
    batch,
):
    session, kwargs = submission
    if batch:
        from inspire.cli.commands.batch import _submit_notebook_plan
        from inspire.cli.utils import notebook_post_start

        monkeypatch.setattr(
            notebook_post_start, "resolve_notebook_post_start_spec", lambda **kw: None
        )
        _submit_notebook_plan(
            {"name": "fresh", "create_kwargs": kwargs}, config=None, session=session
        )
    else:
        api.create_notebook(**kwargs, session=session)
    calls = []

    def listing(*a, **kw):
        calls.append(kw)
        return {} if len(calls) == 1 else {"ws-test": [{"name": "fresh", "notebook_id": "nb-1"}]}

    monkeypatch.setattr(lookup, "_list_notebooks_for_workspaces", listing)
    assert resolve(session, require_live)[:2] == ("nb-1", "ws-test")
    assert len(calls) == 2
    assert clock[1] == [1.0]


@pytest.mark.parametrize(
    "age,account,workspace,expected_wait",
    [
        (30, "alice", "ws-test", 0),
        (0, "bob", "ws-test", 0),
        (0, "alice", "other", 0),
        (0, "alice", "ws-test", 12),
        (28, "alice", "ws-test", 2),
    ],
)
def test_marker_expiry_scope_and_wait_budget(
    monkeypatch,
    clock,
    submission,
    age,
    account,
    workspace,
    expected_wait,
):
    session, _ = submission
    storage.ResourceIndex.for_account(account).record_notebook_creation(workspace, "fresh")
    clock[0][0] += age
    calls = []
    monkeypatch.setattr(
        lookup, "_list_notebooks_for_workspaces", lambda *a, **kw: calls.append(kw) or {}
    )
    with pytest.raises(SystemExit) as exc:
        resolve(session)
    assert exc.value.code == 12
    assert sum(clock[1]) == expected_wait
    if not expected_wait:
        assert len(calls) == 1


def test_recent_marker_does_not_retry_api_errors(monkeypatch, clock, submission):
    session, kwargs = submission
    api.create_notebook(**kwargs, session=session)

    def fail(*a, **kw):
        raise RuntimeError("network failed")

    monkeypatch.setattr(lookup, "_list_notebooks_for_workspaces", fail)
    with pytest.raises(RuntimeError, match="network failed"):
        resolve(session)
    assert clock[1] == []


def test_failed_submission_never_records_marker(monkeypatch, submission):
    session, kwargs = submission

    def fail(*a, **kw):
        raise RuntimeError("API failed")

    monkeypatch.setattr(api, "_notebook_v2", fail)
    with pytest.raises(RuntimeError, match="API failed"):
        api.create_notebook(**kwargs, session=session)
    index = storage.ResourceIndex.for_account("alice")
    assert index.notebook_visibility_remaining("ws-test", "fresh") == 0


def test_markers_persist_and_prune(tmp_path, clock):
    path = tmp_path / "index.sqlite3"
    storage.ResourceIndex(path).record_notebook_creation("ws", "old")
    assert storage.ResourceIndex(path).notebook_visibility_remaining("ws", "old") == 30
    clock[0][0] += 30
    storage.ResourceIndex(path).record_notebook_creation("ws", "new")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM recent_notebook_creation").fetchall() == [
            ("new",)
        ]
