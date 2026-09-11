"""Notebook SDK contracts against fake platform module attributes."""

from __future__ import annotations

from dataclasses import fields, replace
from types import SimpleNamespace

import pytest

from test_sdk import client as client
from inspire import (
    Notebook,
    InspireError,
    Image,
    ImageSelector,
    MetricGroup,
    NotebookResourceSnapshot,
    NotebookCreateSpec,
    NotebookRef,
    NotebookFailedError,
    ImageRef,
    WorkspaceRef,
    Resource,
    Quota,
    ValidationError,
    AmbiguousResourceError,
    SubmissionUncertainError,
    MutationUncertainError,
    WaitTimeoutError,
)
from inspire.platform.web import browser_api as api
from inspire.platform.web.browser_api.projects import ProjectInfo
from inspire.platform.web.browser_api.notebooks import ImageInfo
from inspire.services.catalog.quotas import ResolvedQuota


@pytest.fixture
def catalog(client, monkeypatch):
    ws = Resource(
        "Workspace",
        WorkspaceRef("Workspace", client.account, client.base_url, "ws-test", "ws-test"),
    )
    project = ProjectInfo("p", "Project", "ws-test", priority_name="4")
    image = ImageInfo("i", "registry/image:v1", "Image", "pytorch", "v1")
    group = {"id": "g", "name": "Group", "support_job_type_list": '["interactive_modeling"]'}
    price = {
        "quota_id": "q",
        "gpu_count": 1,
        "cpu_count": 20,
        "memory_size_gib": 200,
        "gpu_info": {"gpu_type": "GPU"},
        "total_price_per_hour": 8,
    }
    calls = []
    monkeypatch.setattr(client.workspaces, "get", lambda _: ws)
    monkeypatch.setattr(api, "get_current_user", lambda **kw: {"id": "user"})
    monkeypatch.setattr(api, "list_notebook_compute_groups", lambda **kw: [group])

    def prices(**kw):
        calls.append(kw)
        return [price]

    monkeypatch.setattr(api, "get_resource_prices", prices)
    monkeypatch.setattr(api, "list_projects", lambda **kw: [project])
    monkeypatch.setattr(api, "list_images", lambda **kw: [image])
    from inspire.platform.web.browser_api.images import CustomImageInfo

    catalog_image = CustomImageInfo(
        "i",
        image.url,
        image.name,
        image.framework,
        image.version,
        "SOURCE_PRIVATE",
        "READY",
        "",
        "",
    )
    monkeypatch.setattr(api, "list_images_by_source", lambda **kw: [catalog_image])
    monkeypatch.setattr(api, "get_image_detail", lambda **kw: catalog_image)
    monkeypatch.setattr(api, "get_quota_priority_levels", lambda **kw: {"q": ()})
    monkeypatch.setattr(api, "check_scheduling_health", lambda **kw: set())
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.workspaces.is_fair_scheduling_workspace", lambda *a: True
    )
    monkeypatch.setattr(api, "notebook_name_exists", lambda *a, **kw: False)
    monkeypatch.setattr(api, "list_notebooks", lambda *a, **kw: ([], 0))
    spec = NotebookCreateSpec(
        "Example", "Workspace", "Project", "Group", Quota(1, 20, 200), "Image"
    )
    return SimpleNamespace(
        ws=ws, project=project, image=image, group=group, price=price, calls=calls, spec=spec
    )


@pytest.fixture
def ref(client):
    return NotebookRef("Example", client.account, client.base_url, "nb", "ws-test")


@pytest.mark.parametrize("advanced", [False, True])
def test_create_kwargs_equal_cli(client, catalog, monkeypatch, advanced):
    from inspire.cli.commands.notebook import notebook_create_flow as flow
    from inspire.cli.context import Context
    from inspire.services.catalog import datasets

    spec = catalog.spec
    if advanced:
        spec = replace(
            spec,
            shm_gib=40,
            auto_stop_after=125,
            datasets=["data:v2"],
            enable_notification=False,
            public_path_readonly=True,
            project_path_readonly=False,
            priority=4,
            node="node-a",
        )
    dataset_info = [{"name": "data", "version": "v2", "path": "/data"}] if advanced else []
    monkeypatch.setattr(datasets, "resolve_dataset_info", lambda *a, **kw: dataset_info)
    calls = []

    def create(**kw):
        calls.append(kw)
        return {"notebook": {"id": "created"}}

    monkeypatch.setattr(api, "create_notebook", create)
    plan = client.notebooks.plan(spec)
    assert calls == []
    handle = client.notebooks.create(spec, operation_id="any-operation-name")
    assert handle.ref.key == "created"
    assert handle.operation_id == "any-operation-name"
    assert len(calls) == 1
    quota = ResolvedQuota("q", "g", "Group", 1, 20, 200, "GPU", catalog.price)
    monkeypatch.setattr(flow, "remember_resource_identity", lambda **kw: None)
    # The command reporter receives independently resolved CLI arguments and calls
    # its own browser_api attribute; do not stub the shared kwargs builder.
    flow.create_notebook_and_report(
        Context(),
        name=spec.name,
        diagnostics=flow.NotebookCreateDiagnostics(
            spec.name, "Workspace", "Project", "Image", "GPU", "Group"
        ),
        selected_project=catalog.project,
        selected_image=catalog.image,
        quota=quota,
        shm_size=40 if advanced else (client._config.shm_size or 32),
        auto_stop=advanced,
        workspace_id="ws-test",
        session=client._transport.session,
        json_output=True,
        task_priority=4,
        node_id="node-a" if advanced else None,
        dataset_info=dataset_info or None,
        enable_notification=False if advanced else None,
        stop_hour=2 if advanced else None,
        stop_minute=5 if advanced else None,
        public_path_readonly=True if advanced else None,
        project_path_readonly=False if advanced else None,
    )
    assert calls[0] == calls[1]
    assert calls[0]["resource_spec_price"]["gpu_type"] == "GPU"
    assert plan.priority == 4
    assert plan.workspace.ref == catalog.ws.ref
    assert plan.project.ref.key == "p"
    assert plan.group.ref.key == "g"
    assert plan.image.ref.key == "i"
    assert plan.to_dict()["image"] == plan.image.name
    assert plan.to_dict()["workspace"] == plan.workspace.name


@pytest.mark.parametrize(
    "action,platform,create",
    [
        ("create", "create_notebook", True),
        ("start", "start_notebook", False),
        ("stop", "stop_notebook", False),
        ("delete", "delete_notebook", False),
        ("save_image", "save_notebook_as_image", False),
        ("cancel_save_image", "cancel_notebook_image_save", False),
    ],
)
def test_mutations_single_dispatch_on_lost_response(
    client, catalog, ref, monkeypatch, action, platform, create
):
    from inspire.platform.web.session.models import SessionExpiredError

    calls = []
    monkeypatch.setattr(api, "estimate_notebook_image_size", lambda **kw: None)

    def once(*a, **kw):
        calls.append(kw)
        raise SessionExpiredError("lost response")

    monkeypatch.setattr(client._transport, "_once", once)

    def send(*a, **kw):
        return client._transport.request("POST", "/fake", body={})

    monkeypatch.setattr(api, platform, send)
    args = (catalog.spec,) if create else (ref,)

    # _once is replaced, so simulate the dispatch boundary itself as transport tests do.
    def dispatched(*a, **kw):
        return client._transport._dispatch(once, *a, **kw)

    monkeypatch.setattr(client._transport, "_once", dispatched)
    with pytest.raises(SubmissionUncertainError if create else MutationUncertainError) as error:
        getattr(client.notebooks, action)(*args, **({"name": "snapshot"} if action == "save_image" else {}))
    assert len(calls) == 1
    if create:
        assert "inspect notebooks" in str(error.value)


@pytest.mark.parametrize(
    "action,platform",
    [
        ("start", "start_notebook"),
        ("stop", "stop_notebook"),
        ("delete", "delete_notebook"),
        ("cancel_save_image", "cancel_notebook_image_save"),
    ],
)
def test_mutations_no_detail_precheck(client, ref, monkeypatch, action, platform):
    calls = []

    def send(**kw):
        assert client._transport._write is not None
        calls.append(kw)
        return True

    monkeypatch.setattr(api, platform, send)
    monkeypatch.setattr(api, "get_notebook_detail", lambda **kw: pytest.fail("unexpected precheck"))
    getattr(client.notebooks, action)(ref)
    assert len(calls) == 1
    assert calls[0]["notebook_id"] == "nb"


def test_create_id_lookup_and_missing_identity(client, catalog, monkeypatch):
    calls = []
    monkeypatch.setattr(api, "create_notebook", lambda **kw: calls.append(kw) or {})
    monkeypatch.setattr(
        api,
        "list_notebooks",
        lambda *a, **kw: ([{"id": "new", "name": "Example", "created_at": "2026"}], 1),
    )
    assert client.notebooks.create(catalog.spec).ref.key == "new"
    assert len(calls) == 1
    monkeypatch.setattr(api, "list_notebooks", lambda *a, **kw: ([], 0))
    with pytest.raises(SubmissionUncertainError, match="inspect notebooks") as error:
        client.notebooks.create(catalog.spec, operation_id="unknown")
    assert error.value.operation_id == "unknown"
    assert len(calls) == 2


def test_name_precheck_advisory(client, catalog, monkeypatch):
    monkeypatch.setattr(api, "notebook_name_exists", lambda *a, **kw: True)
    monkeypatch.setattr(api, "create_notebook", lambda **kw: pytest.fail("duplicate"))
    with pytest.raises(ValidationError, match="already exists"):
        client.notebooks.create(catalog.spec)

    def unavailable(*a, **kw):
        raise ValueError("platform unavailable")

    monkeypatch.setattr(api, "notebook_name_exists", unavailable)
    monkeypatch.setattr(api, "create_notebook", lambda **kw: {"id": "new"})
    assert client.notebooks.create(catalog.spec).ref.key == "new"


@pytest.mark.parametrize("terminal", ["FAILED", "ERROR", "DELETED", "STOPPED"])
def test_wait_terminal_and_failure(client, ref, monkeypatch, terminal):
    monkeypatch.setattr(
        api,
        "get_notebook_detail",
        lambda **kw: {"name": "Example", "status": terminal, "sub_status": "detail"},
    )
    assert client.notebooks.wait(ref).status == terminal
    with pytest.raises(NotebookFailedError) as error:
        client.notebooks.wait(ref, raise_on_failure=True)
    from inspire.sdk import NotebookFailedError as SDKNotebookFailedError
    from inspire.services.notebook.notebook_status import NotebookFailedError as CLINotebookFailedError

    assert isinstance(error.value, InspireError)
    assert error.value.notebook.status == terminal
    assert error.value.notebook.sub_status == "detail"
    assert error.value.notebook.ref == ref
    assert isinstance(error.value.notebook, Notebook)
    assert SDKNotebookFailedError is NotebookFailedError
    assert api.NotebookFailedError is CLINotebookFailedError
    assert CLINotebookFailedError is not NotebookFailedError


@pytest.mark.parametrize(
    "target,states",
    [("RUNNING", ["CREATING", "RUNNING"]), ("STOPPED", ["RUNNING", "STOPPING", "STOPPED"])],
)
def test_wait_targets(client, ref, monkeypatch, target, states):
    values = iter(states)
    monkeypatch.setattr(api, "get_notebook_detail", lambda **kw: {"status": next(values)})
    monkeypatch.setattr("inspire.sdk.notebooks.time.sleep", lambda _: None)
    assert client.notebooks.wait(ref, target=target, raise_on_failure=True).status == target


def test_wait_timeout(client, ref, monkeypatch):
    monkeypatch.setattr(api, "get_notebook_detail", lambda **kw: {"status": "CREATING"})
    with pytest.raises(WaitTimeoutError):
        client.notebooks.wait(ref, timeout=0.01, poll_interval=0.001)
    with pytest.raises(ValidationError):
        client.notebooks.wait(ref, timeout=float("inf"))


def test_discovery_status_and_cursor(client, catalog, monkeypatch):
    rows = [
        {"id": "a", "name": "First", "status": "RUNNING"},
        {"id": "b", "name": "Second", "status": "STOPPED"},
    ]
    calls = []

    def listing(*a, **kw):
        calls.append(kw)
        return rows, 2

    monkeypatch.setattr(api, "list_notebooks", listing)
    page = client.notebooks.list("Workspace", status="running", keyword="First", limit=1)
    assert [x.name for x in page.items] == ["First"]
    assert calls[0]["status"] == ["RUNNING"]
    assert calls[0]["user_ids"] == ["user"]
    assert calls[0]["keyword"] == "First"
    first = client.notebooks.list("Workspace", limit=1)
    assert [
        x.name for x in client.notebooks.list("Workspace", limit=1, cursor=first.next_cursor).items
    ] == ["Second"]
    assert [x.name for x in client.notebooks.iter("Workspace")] == ["First", "Second"]
    with pytest.raises(ValidationError):
        client.notebooks.list("Workspace", status="STOPPED", cursor=first.next_cursor)
    monkeypatch.setattr(
        api,
        "get_notebook_detail",
        lambda **kw: next(x for x in rows if x["id"] == kw["notebook_id"]),
    )
    assert [x.status for x in client.notebooks.status(["first", "SECOND"], workspace="Workspace")] == [
        "RUNNING",
        "STOPPED",
    ]
    rows.append({"id": "c", "name": "First", "status": "RUNNING"})
    with pytest.raises(AmbiguousResourceError):
        client.notebooks.get("first", workspace="Workspace")


def test_detail_fields_match_cli_projection(client, ref, monkeypatch):
    from inspire.services.notebook.notebook_output import public_notebook

    row = {
        "name": "Example",
        "status": "RUNNING",
        "project": {"name": "Project", "priority_name": "4", "priority_level": "high"},
        "logic_compute_group": {"name": "Group"},
        "created_by": {"name": "Owner"},
        "dataset_info": [{"dataset_id": "data", "version_id": "v1"}],
        "workspace": {"name": "Workspace"},
        "image": {"name": "Image", "version": "v1"},
        "quota": {"gpu_count": 1, "cpu_count": 20, "memory_size": 200},
        "node": {"name": "node-a", "status": "RUNNING"},
        "start_config": {"shared_memory_size": 32},
        "live_time": "42",
        "left_time": "100",
        "created_at": "2026-09-01",
        "updated_at": "2026-09-02",
    }
    monkeypatch.setattr(api, "get_notebook_detail", lambda **kw: row)
    nb = client.notebooks.get(ref)
    view = public_notebook(row)
    assert set(view) == {field.name for field in fields(Notebook)} - {
        "ref", "raw_status", "sub_status"
    }
    for key, value in view.items():
        assert getattr(nb, key) == value
    assert nb.quota["memory_gib"] == 200
    assert nb.raw_status == "RUNNING"


def test_events_keyword_limit_follow_no_terminal_exit(client, ref, monkeypatch):
    rows = [
        {"message": "Pull image", "id": 1},
        {"content": "Image ready", "id": 2},
        {"message": "Stopped", "id": 3},
    ]
    monkeypatch.setattr(api, "list_notebook_events", lambda *a, **kw: rows)
    result = client.notebooks.events(ref, keyword="IMAGE", limit=1)
    assert result.items[0]["id"] == 2 and result.truncated
    monkeypatch.setattr(
        api, "get_notebook_detail", lambda **kw: pytest.fail("follow must not exit on state")
    )
    monkeypatch.setattr("inspire.sdk.notebooks.time.sleep", lambda _: None)
    stream = client.notebooks.follow_events(ref)
    assert len(next(stream).items) == 3
    rows.append({"message": "Restarting", "id": 4})
    assert next(stream).items == ({"message": "Restarting", "id": 4},)
    stream.close()


def test_quotas_workload_and_empty(client, catalog, monkeypatch):
    monkeypatch.setattr(
        api,
        "list_notebook_compute_groups",
        lambda **kw: [
            catalog.group,
            {"id": "empty", "name": "Empty"},
            {"id": "train", "name": "Train", "support_job_type_list": '["distributed_training"]'},
        ],
    )
    calls = []

    def prices(**kw):
        calls.append(kw)
        return [catalog.price] if kw["logic_compute_group_id"] == "g" else []

    monkeypatch.setattr(api, "get_resource_prices", prices)
    result = client.notebooks.quotas("Workspace", include_empty=True)
    assert len(result.items) == 2
    assert result.items[0].quota is None
    assert result.items[1].quota == Quota(1, 20, 200)
    assert result.items[1].points_per_hour == 8
    assert result.items[1].allowed_priority_levels == ()
    assert all(c["schedule_config_type"] == "SCHEDULE_CONFIG_TYPE_DSW" for c in calls)
    assert {c["logic_compute_group_id"] for c in calls} == {"g", "empty"}
    assert len(client.notebooks.quotas("Workspace", group="GRO").items) == 1


def test_metrics_and_lifecycle(client, ref, monkeypatch):
    calls = []
    monkeypatch.setattr(
        api, "get_notebook_detail", lambda **kw: {"start_config": {"logic_compute_group_id": "g"}}
    )
    history = MetricGroup("pod", "cpu_usage_rate", "CPU", [])
    monkeypatch.setattr(
        api, "get_resource_metrics_by_time", lambda **kw: calls.append(kw) or [history]
    )
    assert client.notebooks.metrics(ref, metric="cpu,mem", end="3600", window="30m") == (history,)
    assert calls[0]["start_timestamp"] == 1800 and calls[0]["end_timestamp"] == 3600
    assert calls[0]["interval_second"] == 60
    assert calls[0]["metric_types"] == ["cpu_usage_rate", "memory_usage_rate"]
    snapshot = NotebookResourceSnapshot("CPU", 20, 5, 15, 0.25, "")
    monkeypatch.setattr(
        api, "get_notebook_realtime_metrics", lambda **kw: calls.append(kw) or [snapshot]
    )
    assert client.notebooks.realtime_metrics(ref) == (snapshot,)
    assert calls[-1]["notebook_id"] == "nb"
    monkeypatch.setattr(
        api, "list_notebook_runs", lambda *a, **kw: [{"index": 3}, {"index": 1}, {"index": 2}]
    )
    assert [r.index for r in client.notebooks.lifecycle(ref, limit=2)] == [2, 3]


def test_save_image_estimate_visibility_and_wait(client, ref, monkeypatch):
    estimate = SimpleNamespace(notebook_running=True, size_bytes=1024)
    monkeypatch.setattr(api, "estimate_notebook_image_size", lambda **kw: estimate)
    assert client.notebooks.estimate_image_size(ref) is estimate
    calls = []

    def save(**kw):
        assert client._transport._write is not None
        calls.append(kw)
        return {}

    monkeypatch.setattr(api, "save_notebook_as_image", save)
    monkeypatch.setattr(
        api,
        "list_images_by_source",
        lambda **kw: [
            SimpleNamespace(
                name="snapshot",
                version="v1",
                url="registry/snapshot:v1",
                created_at="2026",
                image_id="saved",
            )
        ],
    )

    def update(**kw):
        assert client._transport._write is not None
        calls.append(kw)

    monkeypatch.setattr(api, "update_image", update)
    handle = client.notebooks.save_image(ref, name="snapshot", flatten=True, visibility="project")
    assert len(calls) == 2
    assert calls[0]["flatten"] is True and calls[0]["version"] == "v1"
    assert "visibility" not in calls[0]
    assert calls[1]["visibility"] == "VISIBILITY_PROJECT"
    assert handle.estimated_size_bytes == 1024
    assert handle.ref == ImageRef(
        "snapshot:v1", client.account, client.base_url, "saved", "ws-test"
    )
    monkeypatch.setattr(api, "wait_for_image_ready", lambda **kw: calls.append(kw) or "ready")
    assert client.notebooks.wait_image_ready(handle, timeout=50, poll_interval=2) == "ready"
    assert calls[-1]["image_id"] == "saved"
    estimate.notebook_running = False
    with pytest.raises(ValidationError, match="not running"):
        client.notebooks.save_image(ref, name="snapshot")


def test_save_missing_identity_and_visibility_failure(client, ref, monkeypatch):
    monkeypatch.setattr(api, "estimate_notebook_image_size", lambda **kw: None)
    calls = []
    monkeypatch.setattr(api, "save_notebook_as_image", lambda **kw: calls.append(kw) or {})
    monkeypatch.setattr(api, "list_images_by_source", lambda **kw: [])
    handle = client.notebooks.save_image(ref, name="snapshot", visibility="public")
    assert handle.ref is None and handle.warning
    assert len(calls) == 1
    with pytest.raises(ValidationError, match="identity"):
        client.notebooks.wait_image_ready(handle)


def test_priority_unknown_and_restricted(client, catalog, monkeypatch):
    monkeypatch.setattr(api, "get_quota_priority_levels", lambda **kw: {"q": ("low",)})
    with pytest.raises(ValidationError, match="LOW-priority only"):
        client.notebooks.plan(catalog.spec)
    assert client.notebooks.plan(replace(catalog.spec, priority=1)).priority == 1

    def unavailable(**kw):
        raise ValueError("unavailable")

    monkeypatch.setattr(api, "get_quota_priority_levels", unavailable)
    client.cache.clear()
    assert client.notebooks.plan(catalog.spec).priority == 4


def test_quota_payload_matches_cli_query(client, catalog, monkeypatch):
    from inspire.cli.commands import workload_quota as command

    monkeypatch.setattr(
        command, "CachedPricesLoader", lambda **kw: lambda key: [catalog.price, catalog.price]
    )
    monkeypatch.setattr(command, "load_quota_priority_levels", lambda **kw: {"q": ("low",)})
    monkeypatch.setattr(api, "get_resource_prices", lambda **kw: [catalog.price, catalog.price])
    monkeypatch.setattr(api, "get_quota_priority_levels", lambda **kw: {"q": ("low",)})
    expected = command._query_workspace_quotas(
        session=client._transport.session,
        workspace_id="ws-test",
        workspace_name="Workspace",
        workload="notebook",
        group_filter="gro",
        include_empty=False,
    )
    actual = client.notebooks.quotas("Workspace", group="GRO")
    assert len(actual.items) == 1
    assert [row.to_dict() for row in actual.items] == expected


def test_wait_image_ready_errors_keep_platform_text(client, ref, monkeypatch):
    image_ref = ImageRef("snapshot:v1", client.account, client.base_url, "i", "ws-test")

    def failed(**kw):
        raise ValueError("platform build failure detail")

    monkeypatch.setattr(api, "wait_for_image_ready", failed)
    with pytest.raises(ValidationError, match="platform build failure detail"):
        client.notebooks.wait_image_ready(image_ref)

    def timeout(**kw):
        raise TimeoutError("platform image wait expired")

    monkeypatch.setattr(api, "wait_for_image_ready", timeout)
    with pytest.raises(WaitTimeoutError, match="platform image wait expired"):
        client.notebooks.wait_image_ready(image_ref)


@pytest.mark.parametrize("selector_kind", ["ref", "selector"])
def test_plan_with_image_reference_or_selector(client, catalog, monkeypatch, selector_kind):
    image_ref = ImageRef("Image", client.account, client.base_url, "i", "ws-test")
    image = Image("Image", image_ref, "private", "registry/image:v1")
    selector = image_ref if selector_kind == "ref" else ImageSelector("Image", source="private")
    calls = []

    def get(selected, *, workspace):
        calls.append((selected, workspace))
        return image

    monkeypatch.setattr(client.images, "get", get)
    plan = client.notebooks.plan(replace(catalog.spec, image=selector))
    assert calls == [(selector, catalog.ws.ref)]
    assert plan.image is image
    assert plan.create_kwargs["image_id"] == image_ref.key
    assert plan.create_kwargs["image_url"] == image.url
    assert plan.create_kwargs["project_id"] == catalog.project.project_id
    assert plan.create_kwargs["project_name"] == catalog.project.name


@pytest.mark.parametrize("known_total", [True, False])
def test_notebook_server_paging(client, catalog, monkeypatch, known_total):
    rows = [{"notebook_id": f"nb-{i}", "name": f"nb-{i}", "status": "RUNNING"}
            for i in range(205)]
    calls = []

    def fetch(*args, **kwargs):
        page, size = kwargs["page"], kwargs["page_size"]
        calls.append((page, size))
        assert size == 100
        return rows[(page - 1) * size:page * size], len(rows) if known_total else None

    monkeypatch.setattr(api, "list_notebooks", fetch)
    first = client.notebooks.list("Workspace", limit=5)
    assert len(first.items) == 5 and calls == [(1, 100)]
    assert first.total == (205 if known_total else None)
    second = client.notebooks.list("Workspace", limit=100, cursor=first.next_cursor)
    assert [r.name for r in second.items] == [f"nb-{i}" for i in range(5, 105)]
    assert calls == [(1, 100), (1, 100), (2, 100)]
    calls.clear()
    assert len(tuple(client.notebooks.iter("Workspace", max_items=150))) == 150
    assert calls == [(1, 100), (2, 100)]
    assert client.notebooks.list("Workspace", status="running", limit=5).total is None
