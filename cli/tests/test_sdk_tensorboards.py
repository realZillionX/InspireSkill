"""TensorBoard create confirmation, wait and data contracts."""

from __future__ import annotations
from types import SimpleNamespace
import pytest
import requests
from test_sdk import client as client
from inspire import TensorboardCreateSpec, TensorboardRef, Resource, WorkspaceRef, ProjectRef
from inspire import (
    SubmissionUncertainError,
    MutationUncertainError,
    ValidationError,
    WaitTimeoutError,
)
from inspire.platform.web import browser_api as api
from inspire.platform.web.browser_api.tensorboards import TensorboardInfo
from inspire.services.tensorboard import tensorboards as core


@pytest.fixture
def catalog(client, monkeypatch):
    ws = Resource(
        "Workspace",
        WorkspaceRef("Workspace", client.account, client.base_url, "ws-test", "ws-test"),
    )
    project = Resource(
        "Project", ProjectRef("Project", client.account, client.base_url, "project-test", "ws-test")
    )
    monkeypatch.setattr(client.workspaces, "get", lambda _: ws)
    monkeypatch.setattr(client.projects, "get", lambda *a, **kw: project)
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.availability.list_compute_groups",
        lambda **kw: [
            {"id": "group-test", "name": "Group", "support_job_type_list": ["tensorboard"]}
        ],
    )
    return TensorboardCreateSpec(
        "board", "Workspace", "Project", "Group", "/inspire/runs", auto_stop_hours=2
    )


def board(status="running"):
    return TensorboardInfo.from_api_response(
        dict(
            tb_id="tb-test",
            name="board",
            status="tb_status_" + status,
            tb_summary_path="/inspire/runs",
            url="https://tensorboard.example/app",
            job_name="train",
        )
    )


@pytest.mark.parametrize("action", ["create", "start", "stop", "delete"])
@pytest.mark.parametrize("outcome", ["ok", "timeout", "platform"])
def test_single_dispatch(client, catalog, monkeypatch, action, outcome):
    ref = TensorboardRef("board", client.account, client.base_url, "tb-test", "ws-test")
    monkeypatch.setattr(core, "find_created_board", lambda *a, **kw: board())
    calls = []

    def once(method, path, body, *a, **kw):
        calls.append((path, body))
        assert client._transport._write is not None
        client._transport._write["sent"] = True
        if outcome == "timeout":
            raise requests.ReadTimeout("lost")
        if outcome == "platform":
            return {
                "ResponseMetadata": {
                    "Error": {"Code": "InvalidParameter", "Message": "平台原始错误"}
                }
            }
        return {"Result": {}}

    monkeypatch.setattr(client._transport, "_once", once)

    def invoke():
        return (
            client.tensorboards.create(catalog)
            if action == "create"
            else getattr(client.tensorboards, action)(ref)
        )

    if outcome == "ok":
        result = invoke()
        if action == "create":
            assert result.ref == ref
    elif outcome == "platform":
        with pytest.raises(ValidationError) as exc:
            invoke()
        assert "API error: InvalidParameter" in str(exc.value)
        assert "平台原始错误" in str(exc.value)
        assert "平台原始错误" in str(exc.value.__cause__)
    else:
        with pytest.raises(
            SubmissionUncertainError if action == "create" else MutationUncertainError
        ) as exc:
            invoke()
    assert len(calls) == 1
    assert calls[0][0].endswith("Action=" + action.title() + "Tensorboard")
    if action == "create":
        assert calls[0][1]["auto_stop_time_ms"] == "7200000"
        assert calls[0][1]["tb_summary_path"] == "/inspire/runs"
        assert calls[0][1]["logic_compute_group_id"] == "group-test"


def test_create_find_and_attached_job(client, catalog, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(api, "get_current_user", lambda **kw: {"id": "user-test"})
    monkeypatch.setattr(
        api,
        "list_jobs",
        lambda **kw: (
            [SimpleNamespace(name="train", job_id="job-test", status="RUNNING", created_at="")],
            1,
        ),
    )
    states = iter([[], [board("creating")], [board()]])
    calls = []
    monkeypatch.setattr(api, "list_tensorboards", lambda **kw: ((rows := next(states)), len(rows)))
    monkeypatch.setattr(core.time, "sleep", lambda _: None)
    monkeypatch.setattr(api, "create_tensorboard", lambda **kw: calls.append(kw) or {})
    result = client.tensorboards.create(replace(catalog, job="train"))
    assert result.ref.key == "tb-test" and len(calls) == 1 and calls[0]["job_id"] == "job-test"


@pytest.mark.parametrize("failure", ["missing", "read_error"])
def test_confirmation_uncertain_without_resubmit(client, catalog, monkeypatch, failure):
    calls = []
    monkeypatch.setattr(api, "create_tensorboard", lambda **kw: calls.append(kw) or {})

    def find(*a, **kw):
        if failure == "read_error":
            raise ValueError("confirmation failed")
        return None

    monkeypatch.setattr(core, "find_created_board", find)
    with pytest.raises(SubmissionUncertainError, match="inspect TensorBoards"):
        client.tensorboards.create(catalog)
    assert len(calls) == 1


def test_wait_and_data(client, catalog, monkeypatch):
    ref = TensorboardRef("board", client.account, client.base_url, "tb-test", "ws-test")
    statuses = iter(["creating", "running"])
    monkeypatch.setattr(api, "get_tensorboard", lambda *a, **kw: board(next(statuses)))
    assert client.tensorboards.wait(ref, target="TB_STATUS_RUNNING", poll_interval=0.001).status == "RUNNING"
    monkeypatch.setattr(api, "get_tensorboard", lambda *a, **kw: board())
    monkeypatch.setattr(api, "read_tensorboard_runs", lambda *a, **kw: ["."])
    monkeypatch.setattr(api, "read_tensorboard_scalar_tags", lambda *a, **kw: {".": ["loss"]})
    monkeypatch.setattr(
        api, "read_tensorboard_scalar_series", lambda *a, **kw: [(1.0, 2, 0.2), (2.0, 1, 0.8)]
    )
    assert client.tensorboards.tags(ref).runs == (".",)
    series = client.tensorboards.scalars(ref, tag="loss", points=1).series[0]
    assert (
        series.first_value == 0.8
        and series.last_value == 0.2
        and [point.to_list() for point in series.points] == [[2, 0.2]]
    )
    assert client.tensorboards.url(ref).startswith("https://tensorboard.example/app")
    monkeypatch.setattr(api, "get_tensorboard", lambda *a, **kw: board("stopped"))
    with pytest.raises(ValidationError):
        client.tensorboards.tags(ref)
    with pytest.raises(WaitTimeoutError):
        client.tensorboards.wait(ref, target="running", timeout=0.002, poll_interval=0.001)


def test_list_job_filter(client, catalog, monkeypatch):
    monkeypatch.setattr(api, "list_tensorboards", lambda **kw: ([board()], 1))
    assert client.tensorboards.list("Workspace", job="train").items[0].name == "board"
    assert not client.tensorboards.list("Workspace", job="another").items


def test_batch_status_and_failed_wait(client, catalog, monkeypatch):
    from inspire import TensorboardFailedError

    ref = TensorboardRef("board", client.account, client.base_url, "tb-test", "ws-test")
    monkeypatch.setattr(api, "list_tensorboards", lambda **kw: ([board()], 1))
    monkeypatch.setattr(api, "get_tensorboard", lambda *a, **kw: board())
    assert client.tensorboards.status([]) == ()
    snapshots = client.tensorboards.status(["board", ref], workspace="Workspace")
    assert isinstance(snapshots, tuple) and len(snapshots) == 2
    assert all(snapshot.ref.key == ref.key for snapshot in snapshots)
    monkeypatch.setattr(api, "get_tensorboard", lambda *a, **kw: board("failed"))
    assert client.tensorboards.wait(ref).status == "FAILED"
    with pytest.raises(TensorboardFailedError) as error:
        client.tensorboards.wait(ref, raise_on_failure=True)
    assert error.value.tensorboard.ref == ref


def test_name_resolution_sends_keyword(client, catalog, monkeypatch):
    calls = []

    def fetch(**kwargs):
        calls.append(kwargs)
        return [board()], 1

    monkeypatch.setattr(api, "list_tensorboards", fetch)
    monkeypatch.setattr(api, "get_tensorboard", lambda *a, **kw: board())
    assert client.tensorboards.get("board", workspace="Workspace").ref.key == "tb-test"
    assert len(calls) == 1 and calls[0]["keyword"] == "board"


def test_create_passes_complete_payload(client, catalog, monkeypatch):
    from dataclasses import replace
    from inspire import JobRef

    # TensorBoard create has no CLI --dry-run; pin every API kwarg literally.
    calls = []
    monkeypatch.setattr(api, "create_tensorboard", lambda **kw: calls.append(kw) or {})
    monkeypatch.setattr(core, "find_created_board", lambda *a, **kw: board())
    job = JobRef("train", client.account, client.base_url, "job-test", "ws-test")
    client.tensorboards.create(replace(catalog, job=job))
    assert calls == [{
        "name": "board", "workspace_id": "ws-test", "project_id": "project-test",
        "logic_compute_group_id": "group-test", "summary_path": "/inspire/runs",
        "auto_stop_ms": 7200000, "job_id": "job-test", "session": client._transport.session,
    }]
