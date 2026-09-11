"""HPC SDK/CLI payload, transport, observation and identity contracts."""

from __future__ import annotations
from dataclasses import replace
from importlib import import_module
from types import SimpleNamespace
import json

import pytest
import requests
from click.testing import CliRunner
from test_sdk import client as client
from inspire import (
    HPCJobCreateSpec,
    HPCJobRef,
    HPCJobFailedError,
    RayJobCreateSpec,
    RayJobRef,
    RayJobFailedError,
    Resource,
    WorkspaceRef,
    Quota,
    ValidationError,
    WaitTimeoutError,
    SubmissionUncertainError,
    MutationUncertainError,
    AmbiguousResourceError,
    ResolutionIncompleteError,
    ResourceNotFoundError,
    ServingRef,
)
from inspire.platform.web import browser_api as api
from inspire.platform.web.browser_api.projects import ProjectInfo
from inspire.services.catalog.quotas import ResolvedQuota


@pytest.fixture
def catalog(client, monkeypatch):
    ws = Resource(
        "Workspace",
        WorkspaceRef("Workspace", client.account, client.base_url, "ws-test", "ws-test"),
    )
    project = ProjectInfo("project-test", "Project", "ws-test", priority_name="10")
    group = {"id": "group-test", "name": "Group", "support_job_type_list": '["hpc_job", "ray_job"]'}
    price = {
        "quota_id": "quota-test",
        "gpu_count": 0,
        "cpu_count": 8,
        "memory_size_gib": 32,
        "cpu_info": {"cpu_type": "cpu"},
        "total_price_per_hour": 2,
    }
    resolved = ResolvedQuota("quota-test", "group-test", "Group", 0, 8, 32, "", price)
    monkeypatch.setattr(client.workspaces, "get", lambda _: ws)
    monkeypatch.setattr(api, "list_notebook_compute_groups", lambda **kw: [group])
    monkeypatch.setattr(api, "get_resource_prices", lambda **kw: [price])
    monkeypatch.setattr(api, "list_projects", lambda **kw: [project])
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.projects.list_projects", lambda **kw: [project]
    )
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.workspaces.is_fair_scheduling_workspace", lambda *a: False
    )
    monkeypatch.setattr(
        "inspire.services.catalog.image_resolution.resolve_image_url",
        lambda value, **kw: "registry/image:v1",
    )
    monkeypatch.setattr(
        "inspire.services.hpc.hpc_submission.resolve_image_url", lambda value, **kw: "registry/image:v1"
    )
    monkeypatch.setattr(
        "inspire.services.ray.ray_submission.resolve_image_id", lambda value, **kw: "image-test"
    )
    from inspire.platform.web.browser_api.images import CustomImageInfo

    monkeypatch.setattr(
        api,
        "list_images_by_source",
        lambda **kw: [
            CustomImageInfo(
                "image-test",
                "registry/image:v1",
                "Image",
                "",
                "v1",
                "SOURCE_PRIVATE",
                "READY",
                "",
                "",
            )
        ],
    )
    hpc = HPCJobCreateSpec(
        "example", "srun echo hello", "Workspace", "Project", "Group", Quota(0, 8, 32), "Image"
    )
    ray = RayJobCreateSpec(
        "example",
        "echo hello",
        "Workspace",
        "Project",
        "Group",
        Quota(0, 8, 32),
        "Image",
        workers=[
            "name=decode;image=Image;group=Group;quota=0,8,32;min=1;max=3;shm-size=8;image-type=SOURCE_PRIVATE"
        ],
    )
    return SimpleNamespace(
        ws=ws, project=project, group=group, price=price, resolved=resolved, hpc=hpc, ray=ray
    )


def check_cli_payload(kind, client, catalog, monkeypatch):
    from inspire.cli.main import main
    from inspire.cli.utils import quota_resolver
    from inspire.services.catalog import datasets

    cli = import_module(f"inspire.cli.commands.{kind}.{kind}_commands")
    service = getattr(client, kind)
    spec = catalog.hpc if kind == "hpc" else catalog.ray
    if kind == "hpc":
        spec = replace(
            spec,
            image_type="SOURCE_OFFICIAL",
            instance_count=2,
            priority=6,
            number_of_tasks=4,
            cpus_per_task=4,
            memory_per_cpu=4,
            enable_hyper_threading=True,
            max_time_hours=25.5,
            keep_after_finish_hours=0.5,
            datasets=["dataset:v2"],
            description="description",
            enable_notification=True,
            public_path_readonly=False,
        )
        extra = [
            "--entrypoint",
            spec.entrypoint,
            "--instance-count",
            "2",
            "--number-of-tasks",
            "4",
            "--cpus-per-task",
            "4",
            "--memory-per-cpu",
            "4",
            "--enable-hyper-threading",
            "--max-time",
            "25.5",
            "--keep-after-finish",
            "0.5",
            "--dataset",
            "dataset:v2",
            "--enable-notification",
        ]
        monkeypatch.setattr(cli, "resolve_image_url", lambda raw, **kw: "registry/image:v1")
        data = [{"dataset_id": "data-test", "path": "storage/dataset", "version": "v2"}]
        monkeypatch.setattr(datasets, "resolve_dataset_info", lambda *a, **kw: data)
        monkeypatch.setattr(cli, "resolve_dataset_info", lambda *a, **kw: data)
    else:
        spec = replace(
            spec,
            image_type="SOURCE_OFFICIAL",
            description="description",
            priority=6,
            shm_gib=16,
            public_path_readonly=False,
        )
        extra = ["--command", spec.command, "--shm-size", "16", "--worker", spec.workers[0]]
        monkeypatch.setattr(cli, "_resolve_image_id", lambda raw, **kw: "image-test")
    monkeypatch.setattr(
        cli.Config, "from_files_and_env", classmethod(lambda cls, **kw: (client._config, {}))
    )
    monkeypatch.setattr(cli, "get_web_session", lambda: client._transport._session)
    monkeypatch.setattr(cli, "select_workspace_id", lambda **kw: catalog.ws.ref.key)
    monkeypatch.setattr(cli, "workspace_label", lambda *a: catalog.ws.name)
    monkeypatch.setattr(cli, "_project_label", lambda *a: catalog.project.name)
    monkeypatch.setattr(cli, "_resolve_project_id", lambda *a, **kw: catalog.project.project_id)
    if kind == "hpc":
        monkeypatch.setattr(cli, "_resolve_project_info", lambda *a, **kw: catalog.project)
    monkeypatch.setattr(quota_resolver, "resolve_quota", lambda **kw: catalog.resolved)
    planned = service.plan(spec)
    assert planned.project.name == "Project"
    assert planned.project.ref.key == "project-test"
    assert planned.group.name == "Group"
    assert planned.group.ref.key == "group-test"
    assert planned.quota == Quota(0, 8, 32)
    assert planned.priority == 6
    assert planned.image.url == "registry/image:v1"
    assert planned.image.ref.key == "image-test"
    for text in ("Project", "Group", str(planned.quota), planned.image.name, "priority=6"):
        assert text in planned.summary
    if kind == "hpc":
        assert (planned.instance_count, planned.number_of_tasks,
                planned.cpus_per_task, planned.memory_per_cpu) == (2, 4, 4, 4)
        assert [(mount.dataset, mount.version) for mount in planned.datasets] == [("dataset", "v2")]
    else:
        assert planned.shm_gib == 16
        assert planned.workers[0]["min"] == 1
        assert planned.workers[0]["max"] == 3
        assert planned.workers[0]["shm_size"] == 8

    # Capture the exact body built by the CLI's shared core, in addition to JSON dry-run output.
    captured = []
    fn_name = "build_hpc_create_payload" if kind == "hpc" else "_assemble_create_body"
    original = getattr(cli, fn_name)

    def build(*a, **kw):
        result = original(*a, **kw)
        captured.append(result)
        return result

    monkeypatch.setattr(cli, fn_name, build)
    result = CliRunner().invoke(
        main,
        [
            "--json",
            kind,
            "create",
            "--dry-run",
            "--name",
            "example",
            "--workspace",
            "Workspace",
            "--project",
            "Project",
            "--group",
            "Group",
            "--quota",
            "0,8,32",
            "--image",
            "Image",
            "--image-type",
            "SOURCE_OFFICIAL",
            "--priority",
            "6",
            "--description",
            "description",
            "--no-public-path-readonly",
            *extra,
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured == [planned.create_kwargs]
    # JSON output deliberately scrubs platform paths; apply the same public rendering to SDK plan.
    from inspire.services.utils.json_formatter import format_json

    assert json.loads(result.output) == json.loads(format_json(planned.to_dict()))
    assert (
        spec.entrypoint not in planned.summary
        if kind == "hpc"
        else spec.command not in planned.summary
    )


def test_fully_populated_hpc_payload_equals_cli_dry_run(client, catalog, monkeypatch):
    check_cli_payload("hpc", client, catalog, monkeypatch)


def check_write(kind, action, outcome, client, catalog, monkeypatch):
    service = getattr(client, kind)
    ref_cls = HPCJobRef if kind == "hpc" else RayJobRef
    ref = ref_cls("example", client.account, client.base_url, "job-test", "ws-test")
    plan = service.plan(getattr(catalog, kind))
    monkeypatch.setattr(service, "plan", lambda spec: plan)
    calls = []

    def once(method, path, *args, **kw):
        calls.append((method, path, kw))
        assert client._transport._write is not None
        client._transport._write["sent"] = True
        if outcome == "timeout":
            raise requests.exceptions.ReadTimeout("lost response")
        if outcome == "platform":
            return {
                "ResponseMetadata": {
                    "Error": {"Code": "InvalidParameter", "Message": "平台原始错误"}
                }
            }
        return {
            "Result": {}
            if outcome == "missing"
            else {"job_id" if kind == "hpc" else "ray_job_id": "job-test"}
        }

    monkeypatch.setattr(client._transport, "_once", once)
    monkeypatch.setattr(client._transport, "_refresh", lambda: pytest.fail("write refreshed"))
    client._transport.allow_browser = True

    def invoke():
        if action == "create":
            return service.create(getattr(catalog, kind), operation_id="caller/diagnostic")
        return getattr(service, action)(ref)

    if outcome == "platform" and kind == "hpc":
        with pytest.raises(ValidationError) as caught:
            invoke()
        assert "API error: InvalidParameter" in str(caught.value)
        assert "平台原始错误" in str(caught.value)
        assert "平台原始错误" in str(caught.value.__cause__)
    elif outcome != "ok":
        error = SubmissionUncertainError if action == "create" else MutationUncertainError
        with pytest.raises(error) as caught:
            invoke()
        if outcome == "platform":
            assert "平台原始错误" in str(caught.value.__cause__)
        if action == "create":
            assert caught.value.operation_id == "caller/diagnostic"
    else:
        result = invoke()
        if action == "create":
            assert result.ref == ref
    assert len(calls) == 1
    assert calls[0][1].endswith(
        "Action="
        + ("CreateJobConsole" if kind == "hpc" and action == "create" else action.title() + "Job")
    )


@pytest.mark.parametrize("action", ["create", "stop", "delete"])
@pytest.mark.parametrize("outcome", ["ok", "timeout", "platform"])
def test_hpc_writes_single_dispatch(client, catalog, monkeypatch, action, outcome):
    check_write("hpc", action, outcome, client, catalog, monkeypatch)


def test_hpc_missing_create_identity_is_uncertain(client, catalog, monkeypatch):
    check_write("hpc", "create", "missing", client, catalog, monkeypatch)


def check_wait(kind, client, monkeypatch):
    service = getattr(client, kind)
    ref_cls, error_cls = (
        (HPCJobRef, HPCJobFailedError) if kind == "hpc" else (RayJobRef, RayJobFailedError)
    )
    ref = ref_cls("example", client.account, client.base_url, "job-test", "ws-test")
    api_mod = import_module(f"inspire.platform.web.browser_api.{kind}_jobs")
    values = iter(["UNKNOWN", "pending", "RUNNING", "SUCCEEDED"])
    monkeypatch.setattr(
        api_mod,
        f"get_{kind}_job_detail",
        lambda *a, **kw: {"name": "example", "status": next(values)},
    )
    monkeypatch.setattr("inspire.sdk.compute_jobs.time.sleep", lambda _: None)
    assert service.wait(ref, raise_on_failure=True).status == "SUCCEEDED"
    for status in ("FAILED", "STOPPED", "CANCELLED", "ERROR", "DELETED"):
        monkeypatch.setattr(api_mod, f"get_{kind}_job_detail", lambda *a, **kw: {"status": status})
        assert service.wait(ref).status == status
        with pytest.raises(error_cls) as caught:
            service.wait(ref, raise_on_failure=True)
        assert caught.value.job.ref == ref
    monkeypatch.setattr(api_mod, f"get_{kind}_job_detail", lambda *a, **kw: {"status": "UNKNOWN"})
    with pytest.raises(WaitTimeoutError):
        service.wait(ref, timeout=0.005, poll_interval=0.001)
    with pytest.raises(ValidationError):
        service.wait(ref, poll_interval=0)


def test_hpc_wait_vocabulary(client, monkeypatch):
    check_wait("hpc", client, monkeypatch)


def check_logs(kind, client, monkeypatch, head):
    service = getattr(client, kind)
    ref_cls = HPCJobRef if kind == "hpc" else RayJobRef
    ref = ref_cls("example", client.account, client.base_url, "job-test", "ws-test")
    api_mod = import_module(f"inspire.platform.web.browser_api.{kind}_jobs")
    cli_log = import_module(f"inspire.cli.commands.{kind}.{kind}_logs")
    cli_cmd = import_module(f"inspire.cli.commands.{kind}.{kind}_commands")
    instances = [
        {
            "name": "namespace/pod",
            "role": "launcher" if kind == "hpc" else "head",
            "created_at": "1000000",
            "finished_at": "2000000",
        }
    ]
    monkeypatch.setattr(api, f"list_{kind}_job_instances", lambda *a, **kw: (instances, 1))
    monkeypatch.setattr(
        api_mod,
        f"get_{kind}_job_detail",
        lambda *a, **kw: {"created_at": "1000000", "finished_at": "2000000"},
    )
    rows = [{"pod_name": "pod", "timestamp_ms": i, "message": f"line {i}"} for i in (3, 1, 4, 2)]
    calls = []

    def fetch(**kw):
        calls.append(kw)
        return rows[: kw["page_size"]], len(rows)

    monkeypatch.setattr(api_mod, f"list_{kind}_job_logs", fetch)
    views = getattr(cli_cmd, f"{kind}_instance_views")(instances)
    result = service.logs(
        ref, instance=views[0].label, head=2 if head else None, tail=None if head else 2, limit=2
    )
    expected_range = (
        cli_log._log_time_range(instances, None)
        if kind == "hpc"
        else cli_log._clamped_window({"created_at": "1000000", "finished_at": "2000000"}, None)
    )
    assert (calls[0]["start_timestamp_ms"], calls[0]["end_timestamp_ms"]) == expected_range[:2]
    from inspire.services.job.job_logs import select_job_logs

    fetched = rows if kind == "hpc" and not head else rows[:2]
    expected = select_job_logs(
        cli_log._labelled_logs(fetched, views),
        total=4,
        tail=None if head else 2,
        head=2 if head else None,
        record_limit=2,
        all_output=False,
    )
    assert result.items == tuple(expected.logs)
    assert [c["page_size"] for c in calls] == ([2, 4] if kind == "hpc" and not head else [2])
    assert calls[0]["pod_names"] == ["namespace/pod"]
    sdk_views = service.instances(ref)
    assert [(v.label, v.handle, v.role) for v in views] == [
        (v.label, v.handle, v.role) for v in sdk_views
    ]
    assert [v.raw for v in sdk_views] == instances
    calls.clear()
    service.logs(ref, window="100d")
    assert calls[0]["end_timestamp_ms"] - calls[0]["start_timestamp_ms"] == 30 * 24 * 3600 * 1000
    with pytest.raises(ValidationError):
        service.logs(ref, head=1, tail=1)


@pytest.mark.parametrize("head", [False, True])
def test_hpc_logs_and_instances_match_cli(client, monkeypatch, head):
    check_logs("hpc", client, monkeypatch, head)


def check_discovery(kind, client, catalog, monkeypatch):
    service = getattr(client, kind)
    module = import_module(f"inspire.platform.web.browser_api.{kind}_jobs")
    info = module.HPCJobInfo if kind == "hpc" else module.RayJobInfo
    key = "job_id" if kind == "hpc" else "ray_job_id"
    rows = [
        info.from_api_response(
            {key: f"key{i}", "name": f"item{i}", "status": "RUNNING", "workspace_id": "ws-test"}
        )
        for i in range(105)
    ]
    monkeypatch.setattr(
        module,
        f"list_{kind}_jobs",
        lambda **kw: (rows[(kw["page_num"] - 1) * kw["page_size"] : kw["page_num"] * kw["page_size"]], len(rows)),
    )
    assert len(tuple(service.iter("Workspace"))) == 105
    page = service.list("Workspace", keyword="item10", limit=2)
    assert len(page.items) == 2 and page.next_cursor
    assert len(service.list("Workspace", status="stopped").items) == 0
    selected = service._resolve("ITEM100", "Workspace")
    assert selected.key == "key100"
    assert type(selected).from_dict(selected.to_dict()) == selected
    monkeypatch.setattr(
        module, f"get_{kind}_job_detail", lambda *a, **kw: {"name": "item100", "status": "FAILED"}
    )
    if kind == "hpc":
        monkeypatch.setattr(
            module, "list_hpc_jobs_by_ids",
            lambda keys, **kw: {key: {"name": "item100", "status": "FAILED"} for key in keys},
        )
    assert service.status([selected])[0].status == "FAILED"
    rows[0].name = "item100"
    with pytest.raises(AmbiguousResourceError):
        service.get("item100", workspace="Workspace")
    monkeypatch.setattr(module, f"list_{kind}_jobs", lambda **kw: (rows[:100], 201))
    with pytest.raises(ResolutionIncompleteError):
        service.list("Workspace", limit=200)
    assert service.quotas("Workspace").items[0].quota == Quota(0, 8, 32)


def test_hpc_discovery_quota_and_identity(client, catalog, monkeypatch):
    check_discovery("hpc", client, catalog, monkeypatch)


def check_events_metrics(kind, client, monkeypatch):
    service = getattr(client, kind)
    ref_cls = HPCJobRef if kind == "hpc" else RayJobRef
    ref = ref_cls("example", client.account, client.base_url, "key", "ws-test")
    module = import_module(f"inspire.platform.web.browser_api.{kind}_jobs")
    instance_rows = [{"name": "ns/pod", "role": "worker", "instance_type": "worker"}]
    monkeypatch.setattr(api, f"list_{kind}_job_instances", lambda *a, **kw: (instance_rows, 1))
    job_events = [
        {"reason": "Created", "type": "Normal", "object_type": "job", "last_timestamp": "1"}
    ]
    pod_events = [
        {
            "reason": "FailedScheduling",
            "type": "Warning",
            "object_type": "instance",
            "object_id": "ns/pod",
            "last_timestamp": "2",
        }
    ]
    if kind == "hpc":
        monkeypatch.setattr(module, "list_hpc_job_events", lambda *a, **kw: job_events.copy())
        monkeypatch.setattr(module, "list_hpc_instance_events", lambda *a, **kw: pod_events.copy())
    else:
        monkeypatch.setattr(
            api,
            "list_ray_job_events",
            lambda *a, **kw: pod_events.copy() if kw.get("pod_names") else job_events + pod_events,
        )
    result = service.events(ref, reason="sched", instance="worker")
    assert len(result.items) == 1 and result.items[0]["instance"] == "worker"
    assert [x["reason"] for x in service.events(ref, workload_level=True).items] == ["Created"]
    assert service.events(ref, limit=1).truncated
    if kind == "ray":
        assert service.events(ref, type="warning").items[0]["reason"] == "FailedScheduling"
    with pytest.raises(ValidationError):
        service.events(ref, instance="worker", workload_level=True)
    follow = service.follow_events(ref, interval=0.01)
    assert len(next(follow).items) == 2
    pod_events.append(
        {
            "reason": "Scheduled",
            "object_type": "instance",
            "object_id": "ns/pod",
            "last_timestamp": "3",
        }
    )
    assert [x["reason"] for x in next(follow).items] == ["Scheduled"]
    follow.close()
    calls = []

    def metrics(**kw):
        calls.append(kw)
        return []

    metrics_api = import_module("inspire.platform.web.browser_api.metrics")
    monkeypatch.setattr(metrics_api, "get_resource_metrics_by_time", metrics)
    monkeypatch.setattr(
        module,
        f"get_{kind}_job_detail",
        lambda *a, **kw: (
            {"logic_compute_group_id": "group-key"}
            if kind == "hpc"
            else {"head_node": {"logic_compute_group_id": "group-key"}}
        ),
    )
    from datetime import datetime, timezone

    assert (
        service.metrics(
            ref,
            metric="cpu",
            start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        == ()
    )
    assert calls[0]["logic_compute_group_id"] == "group-key"
    assert calls[0]["task_type"] == metrics_api.TASK_TYPE_BY_RESOURCE[kind]
    assert calls[0]["end_timestamp"] - calls[0]["start_timestamp"] == 86400


def test_hpc_event_filters_follow_and_metrics(client, monkeypatch):
    check_events_metrics("hpc", client, monkeypatch)


def test_hpc_plan_failure_does_not_dispatch(client, catalog, monkeypatch):
    monkeypatch.setattr(
        client._transport, "_once", lambda *a, **kw: pytest.fail("invalid plan sent")
    )
    with pytest.raises(ValidationError, match="exceeds"):
        client.hpc.create(replace(catalog.hpc, cpus_per_task=100))


def test_hpc_read_error_keeps_platform_text(client, monkeypatch):
    ref = HPCJobRef("example", client.account, client.base_url, "key", "ws-test")

    def fail(*a, **kw):
        raise ValueError("平台明文错误")

    monkeypatch.setattr("inspire.platform.web.browser_api.hpc_jobs.get_hpc_job_detail", fail)
    with pytest.raises(ValidationError, match="平台明文错误"):
        client.hpc.get(ref)


def test_hpc_spec_defaults_match_cli_options():
    from dataclasses import fields
    from inspire.cli.main import main as cli
    from inspire import HPCJobCreateSpec

    options = {option.name: option.default for option in cli.commands["hpc"].commands["create"].params}
    defaults = {item.name: item.default for item in fields(HPCJobCreateSpec)}
    for name in ['image_type', 'instance_count', 'number_of_tasks', 'enable_hyper_threading', 'enable_notification', 'priority', 'public_path_readonly', 'description']:
        option_name = "shm_size" if name == "shm_gib" else name
        assert (defaults[name] if defaults[name] is not None else "") == (
            options[option_name] if options[option_name] is not None else ""
        ), name


def test_hpc_list_respects_action_page_size(client, catalog, monkeypatch):
    """A small catalog must never receive the old, rejected page_size=100."""
    from inspire.platform.web.browser_api.hpc_jobs import HPCJobInfo
    from inspire.platform.web.browser_api import hpc_jobs

    rows = [HPCJobInfo.from_api_response({"job_id": f"hpc-{i}", "name": f"job-{i}"})
            for i in range(45)]
    calls = []

    def fetch(**kwargs):
        calls.append((kwargs["page_num"], kwargs["page_size"]))
        if kwargs["page_size"] > 50:
            raise ValueError("InvalidParameter: page or page_size too large")
        return rows[:kwargs["page_size"]], len(rows)

    monkeypatch.setattr(hpc_jobs, "list_hpc_jobs", fetch)
    page = client.hpc.list(catalog.ws.ref, limit=50)
    assert len(page.items) == page.total == 45
    assert len(tuple(client.hpc.iter(catalog.ws.ref))) == 45
    assert calls and all(call == (1, 50) for call in calls)


@pytest.mark.parametrize("kind,size", [("hpc", 50), ("ray", 20)])
def test_workload_server_paging(client, catalog, monkeypatch, kind, size):
    module = import_module(f"inspire.platform.web.browser_api.{kind}_jobs")
    info = module.HPCJobInfo if kind == "hpc" else module.RayJobInfo
    key = "job_id" if kind == "hpc" else "ray_job_id"
    rows = [info.from_api_response({key: f"key-{i}", "name": f"job-{i}"})
            for i in range(200)]
    calls = []

    def fetch(**kwargs):
        page, count = kwargs["page_num"], kwargs["page_size"]
        assert count == size
        calls.append((page, count))
        return rows[(page - 1) * count:page * count], len(rows)

    monkeypatch.setattr(module, f"list_{kind}_jobs", fetch)
    service = getattr(client, kind)
    first = service.list(catalog.ws.ref, limit=5)
    assert [row.name for row in first.items] == [f"job-{i}" for i in range(5)]
    assert first.total == 200 and calls == [(1, size)]
    second = service.list(catalog.ws.ref, limit=size, cursor=first.next_cursor)
    assert [row.name for row in second.items] == [f"job-{i}" for i in range(5, size + 5)]
    assert calls == [(1, size), (1, size), (2, size)]
    calls.clear()
    assert len(tuple(service.iter(catalog.ws.ref, max_items=60))) == 60
    assert calls == [(i, size) for i in range(1, (60 + size - 1) // size + 1)]
    filtered = service.list(catalog.ws.ref, keyword="job-19", limit=5)
    assert filtered.total is None
    assert [row.name for row in filtered.items] == ["job-19", *[f"job-{i}" for i in range(190, 194)]]


@pytest.mark.parametrize("kind,size", [("hpc", 50), ("ray", 20)])
def test_workload_name_scan_is_bounded(client, catalog, monkeypatch, kind, size):
    module = import_module(f"inspire.platform.web.browser_api.{kind}_jobs")
    info = module.HPCJobInfo if kind == "hpc" else module.RayJobInfo
    key = "job_id" if kind == "hpc" else "ray_job_id"
    calls = []

    def fetch(**kwargs):
        page = kwargs["page_num"]
        assert kwargs["page_size"] == size
        assert "keyword" not in kwargs  # Not supported by either Action.
        calls.append(page)
        return [info.from_api_response({key: f"{page}-{i}", "name": "other"})
                for i in range(size)], 89593

    monkeypatch.setattr(module, f"list_{kind}_jobs", fetch)
    with pytest.raises(ResolutionIncompleteError, match="100 pages"):
        getattr(client, kind).get("name", workspace="Workspace")
    assert calls == list(range(1, 101))


@pytest.mark.parametrize("kind,size", [("hpc", 50), ("ray", 20)])
def test_workload_name_candidates_and_duplicate_ids(client, catalog, monkeypatch, kind, size):
    module = import_module(f"inspire.platform.web.browser_api.{kind}_jobs")
    info = module.HPCJobInfo if kind == "hpc" else module.RayJobInfo
    key = "job_id" if kind == "hpc" else "ray_job_id"
    rows = [info.from_api_response({key: f"key-{i}", "name": "prefix-name"})
            for i in range(size)]
    rows += [info.from_api_response({key: "chosen", "name": "name"})] * 2

    def fetch(**kwargs):
        page = kwargs["page_num"]
        return rows[(page - 1) * size:page * size], len(rows)

    monkeypatch.setattr(module, f"list_{kind}_jobs", fetch)
    monkeypatch.setattr(module, f"get_{kind}_job_detail", lambda *a, **kw: {
        key: "chosen", "name": "name", "status": "RUNNING",
    })
    service = getattr(client, kind)
    assert service.get("name", workspace="Workspace").ref.key == "chosen"
    rows.append(info.from_api_response({key: "other", "name": "NAME"}))
    with pytest.raises(AmbiguousResourceError) as error:
        service.get("name", workspace="Workspace")
    assert {row.ref.key for row in error.value.candidates} == {"chosen", "other"}


@pytest.mark.parametrize("kind,size", [("hpc", 50), ("ray", 20)])
def test_workload_filtered_cursor_and_missing_page(client, catalog, monkeypatch, kind, size):
    module = import_module(f"inspire.platform.web.browser_api.{kind}_jobs")
    info = module.HPCJobInfo if kind == "hpc" else module.RayJobInfo
    key = "job_id" if kind == "hpc" else "ray_job_id"
    rows = [info.from_api_response({key: f"key-{i}", "name": f"job-{i}",
            "status": "RUNNING" if i % 3 == 0 else "STOPPED",
            "created_by": {"name": "owner"}}) for i in range(200)]
    calls = []

    def fetch(**kwargs):
        calls.append(kwargs)
        page = kwargs["page_num"]
        return rows[(page - 1) * size:page * size], len(rows)

    monkeypatch.setattr(module, f"list_{kind}_jobs", fetch)
    service = getattr(client, kind)
    first = service.list("Workspace", status="running", keyword="owner", limit=5)
    second = service.list("Workspace", status="running", keyword="owner", limit=5,
                          cursor=first.next_cursor)
    assert [r.name for r in first.items + second.items] == [f"job-{i}" for i in range(0, 30, 3)]
    assert first.total is second.total is None
    if kind == "hpc":
        assert calls[0]["status"] == "RUNNING"
    with pytest.raises(ValidationError, match="Cursor"):
        service.list("Workspace", status="stopped", cursor=first.next_cursor)
    monkeypatch.setattr(module, f"list_{kind}_jobs", lambda **kw: ([], 200))
    with pytest.raises(ResolutionIncompleteError, match="omitted"):
        service.list("Workspace")


def test_platform_page_guard_matches_platform_row_cap():
    from inspire.sdk.resources import platform_page, PLATFORM_MAX_ROWS

    assert PLATFORM_MAX_ROWS == 5000
    assert platform_page(100, 50) == 100
    assert platform_page(50, 100) == 50
    for page, size in ((101, 50), (51, 100), (251, 20)):
        with pytest.raises(ResolutionIncompleteError, match="5000 rows"):
            platform_page(page, size)


def test_hpc_iter_stops_before_platform_row_cap(client, catalog, monkeypatch):
    # The platform rejects page_num * page_size > 5000 with InvalidParameter;
    # the SDK refuses to dispatch that request and names the remedy instead.
    module = import_module("inspire.platform.web.browser_api.hpc_jobs")
    calls = []

    def fetch(**kwargs):
        page = kwargs["page_num"]
        assert kwargs["page_size"] == 50
        calls.append(page)
        return [module.HPCJobInfo.from_api_response({"job_id": f"{page}-{i}", "name": f"n{page}-{i}"})
                for i in range(50)], 89593

    monkeypatch.setattr(module, "list_hpc_jobs", fetch)
    seen = []
    with pytest.raises(ResolutionIncompleteError, match="5000 rows"):
        for job in client.hpc.iter("Workspace"):
            seen.append(job)
    assert len(seen) == 5000
    assert calls == list(range(1, 101))
    assert len(list(client.hpc.iter("Workspace", max_items=5000))) == 5000


@pytest.mark.parametrize("workspace_ids", [("ws-test",), ("ws-test", "ws-other")])
def test_hpc_status_batches_in_input_order(client, monkeypatch, workspace_ids):
    module = import_module("inspire.platform.web.browser_api.hpc_jobs")
    refs = [
        HPCJobRef(str(i), client.account, client.base_url, f"key{i}", ws)
        for ws in workspace_ids for i in reversed(range(21))
    ]
    refs.insert(1, refs[-1])
    calls = []

    def detail(key, *, session):
        return {"job_id": key, "name": key, "status": "RUNNING", "priority": 6,
                "entrypoint": "echo hello", "project_name": "Project"}

    def batch(keys, *, workspace_id, session):
        calls.append((list(keys), workspace_id))
        assert session is client._transport._session
        return {key: detail(key, session=session) for key in reversed(keys)}

    monkeypatch.setattr(module, "list_hpc_jobs_by_ids", batch)
    monkeypatch.setattr(module, "get_hpc_job_detail", detail)
    expected = tuple(client.hpc.get(ref) for ref in refs)
    monkeypatch.setattr(module, "get_hpc_job_detail", lambda *a, **kw: pytest.fail("detail fan-out"))
    result = client.hpc.status(refs)
    assert calls == [
        ([ref.key for ref in refs if ref.workspace_id == ws], ws) for ws in workspace_ids
    ]
    assert tuple(job.ref for job in result) == tuple(refs)
    assert result == expected
    assert all(job.raw == other.raw and job.view == other.view
               for job, other in zip(result, expected))
    calls.clear()
    assert client.hpc.status([]) == ()
    assert calls == []


@pytest.mark.parametrize("record", [None, {"workspace_id": "ws-other"}])
def test_hpc_status_errors_match_get(client, monkeypatch, record):
    module = import_module("inspire.platform.web.browser_api.hpc_jobs")
    ref = HPCJobRef("job", client.account, client.base_url, "key", "ws-test")
    monkeypatch.setattr(module, "get_hpc_job_detail", lambda *a, **kw: record)
    monkeypatch.setattr(
        module, "list_hpc_jobs_by_ids", lambda keys, **kw: {} if record is None else {"key": record}
    )
    error = ResourceNotFoundError if record is None else ValidationError
    with pytest.raises(error) as detail_error:
        client.hpc.get(ref)
    with pytest.raises(error) as batch_error:
        client.hpc.status([ref])
    assert str(batch_error.value) == str(detail_error.value)


@pytest.mark.parametrize("kind,ref_type", [("ray", RayJobRef), ("servings", ServingRef)])
def test_status_without_batch_hook_keeps_detail_fanout(client, monkeypatch, kind, ref_type):
    service = getattr(client, kind)
    refs = [ref_type(key, client.account, client.base_url, key, "ws-test")
            for key in ("second", "first", "second")]
    calls = []

    def detail(key, *, session):
        calls.append(key)
        return {"name": key, "status": "RUNNING"}

    assert service._binding.get_details_by_ids is None
    monkeypatch.setattr(service, "_binding", replace(service._binding, get_detail=detail))
    result = service.status(refs)
    assert calls == [ref.key for ref in refs]
    assert tuple(job.ref for job in result) == tuple(refs)
    assert result == tuple(service.get(ref) for ref in refs)
    calls.clear()
    assert service.status([]) == ()
    assert calls == []
