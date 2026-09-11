"""Cross-facade contracts and documentation checked against actual classes."""
from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import inspect
from pathlib import Path
import re
from typing import get_type_hints

import pytest
from inspire import InspireAsyncClient, Instance, ValidationError
from inspire.sdk.instances import sdk_instances
from test_sdk import client as client


def public_methods(value):
    return {name for name, method in inspect.getmembers(type(value), inspect.isroutine)
            if not name.startswith("_")}


def method_inventory(sync_client, async_client):
    return {name: (public_methods(value), public_methods(getattr(async_client, name)))
            for name, value in vars(sync_client).items()
            if not name.startswith("_")
            and not isinstance(value, (str, int, float, bool, type(None)))
            and public_methods(value)}


def test_documented_method_counts(client):
    async_client = InspireAsyncClient("alpha")
    try:
        inventory = method_inventory(client, async_client)
        document = (Path(__file__).parents[2] / "references/sdk.md").read_text(encoding="utf-8")
        section = document.split("## 各门面方法表", 1)[1].split("### workspaces", 1)[0]
        documented = {name: (int(sync), int(asynchronous)) for name, sync, asynchronous in
                      re.findall(r"\| `([^`]+)` \| (\d+) \| (\d+) \|", section)}
        counts = {name: (len(sync), len(asynchronous))
                  for name, (sync, asynchronous) in inventory.items()}
        assert documented == counts
        resource_sync = sum(pair[0] for name, pair in counts.items() if name != "cache")
        resource_async = sum(pair[1] for name, pair in counts.items() if name != "cache")
        assert f"{len(counts) - 1} 个资源门面" in section
        assert f"{resource_sync} 个同步方法、{resource_async} 个异步方法" in section
        assert f"合计 {resource_sync + counts['cache'][0]}／{resource_async + counts['cache'][1]} 个方法" in section
        extras = [method for sync, asynchronous in inventory.values() for method in asynchronous - sync]
        assert f"多出的 {len(extras)} 个是" in section
        for method in set(extras):
            assert f"{extras.count(method)} 个 {method}" in section
    finally:
        asyncio.run(async_client.close())


@pytest.mark.parametrize("facade", ["jobs", "hpc", "ray", "servings"])
def test_instance_signatures(client, facade):
    service = getattr(client, facade)
    assert get_type_hints(service.instances)["return"] == tuple[Instance, ...]
    assert get_type_hints(service.instance_names)["return"] == tuple[str, ...]
    params = inspect.signature(service.logs).parameters
    assert "instances" not in params
    assert params["instance"].default is None
    for method in (service.exec, service.logs):
        assert "label or handle" in inspect.getdoc(method)


@pytest.mark.parametrize("kind", ["job", "hpc", "ray", "serving"])
def test_sdk_instance_projection_preserves_identity_and_hides_handles(kind):
    handle = "project-12345678-1234-1234-1234-123456789abc/replica-0"
    raw = {"name": handle, "rank": 3, "role": "worker", "type": "worker",
           "status": "Running", "node_name": "node-a"}
    view, = sdk_instances(kind, [raw])
    assert view.handle == handle and view.raw == raw and view.raw is not raw
    assert view.label and view.label != handle
    assert view.status == "Running" and view.node == "node-a" and view.rank == 3
    assert handle not in repr(view)
    with pytest.raises(FrozenInstanceError):
        view.label = "changed"


@pytest.mark.parametrize("facade,accepted", [
    ("notebooks", "RUNNING"), ("servings", "STOPPED"), ("tensorboards", "CREATING"),
])
def test_wait_rejects_unknown_before_resolving(client, monkeypatch, facade, accepted):
    service = getattr(client, facade)
    monkeypatch.setattr(service, "_resolve", lambda *a: pytest.fail("must validate before lookup"))
    with pytest.raises(ValidationError, match=accepted):
        service.wait("name", target="typo")


@pytest.mark.parametrize("facade,target", [
    ("notebooks", " running "), ("servings", " rUnNiNg "),
    ("tensorboards", " Tb_Status_Running "),
])
def test_wait_normalizes_target(client, monkeypatch, facade, target):
    from types import SimpleNamespace
    service = getattr(client, facade)
    snapshot = SimpleNamespace(status="RUNNING")
    monkeypatch.setattr(service, "_resolve", lambda *a: "bound")
    monkeypatch.setattr(service, "get", lambda *a: snapshot)
    assert service.wait("name", target=target, timeout=1) is snapshot


def test_image_waits_share_signature(client):
    image = inspect.signature(client.images.wait_ready)
    notebook = inspect.signature(client.notebooks.wait_image_ready)
    assert image == notebook
    assert get_type_hints(client.images.wait_ready) == get_type_hints(client.notebooks.wait_image_ready)


@pytest.mark.parametrize("facade,kind,ref_type", [
    ("jobs", "job", "JobRef"), ("hpc", "hpc", "HPCJobRef"),
    ("ray", "ray", "RayJobRef"), ("servings", "serving", "ServingRef"),
])
def test_logs_and_exec_accept_public_labels_and_opaque_handles(client, monkeypatch, facade, kind, ref_type):
    from dataclasses import replace
    from datetime import datetime, timezone
    import inspire
    from inspire.platform.web import browser_api as api
    from inspire.sdk import remote_exec
    from inspire.services.execution.remote_exec import ExecResult

    raw = [{"name": f"project-12345678-1234-1234-1234-123456789abc/pod-{i}", "role": "worker", "rank": i,
            "type": "worker", "status": "instance_running"} for i in range(2)]
    service = getattr(client, facade)
    ref = getattr(inspire, ref_type)("test", client.account, client.base_url, "job-test", "ws-test")
    calls = []
    def fetch(*a, **kw):
        calls.append(kw["pod_names"])
        return [], 0
    if kind == "job":
        monkeypatch.setattr("inspire.services.job.job_events.list_all_job_instances", lambda *a, **kw: raw)
        monkeypatch.setattr(service, "get", lambda *a, **kw: inspire.Job("test", ref, "RUNNING", "RUNNING"))
        monkeypatch.setattr("inspire.services.job.job_logs.fetch_job_logs", fetch)
    elif kind == "serving":
        monkeypatch.setattr(api, "list_serving_instances", lambda *a, **kw: (raw, len(raw)))
        monkeypatch.setattr(api, "list_serving_logs", fetch)
    else:
        monkeypatch.setattr(api, f"list_{kind}_job_instances", lambda *a, **kw: (raw, len(raw)))
        monkeypatch.setattr(service, "_binding", replace(
            service._binding, fetch_instances=lambda *a, **kw: (raw, len(raw)), list_logs=fetch,
        ))
    views = service.instances(ref)
    assert all(view.label != view.handle for view in views)
    assert service.instance_names(ref) == tuple(view.label for view in views)
    bounds = dict(start=datetime(2026, 1, 1, tzinfo=timezone.utc), end=datetime(2026, 1, 2, tzinfo=timezone.utc))
    for selector in (views[0].label, views[0].handle, [views[0].label], [views[0].handle]):
        service.logs(ref, instance=selector, **bounds)
        assert calls[-1] == [views[0].handle]
    for selector in (None, "all", [views[1].label, views[0].handle]):
        service.logs(ref, instance=selector, **bounds)
        assert set(calls[-1]) == {v.handle for v in views}
    with pytest.raises(ValidationError):
        service.logs(ref, instance="no-such-label", **bounds)
    monkeypatch.setattr(remote_exec, "authenticated_exec", lambda *a, **kw: ExecResult(0, "ok", "ok", "", True, "pty"))
    selected = []
    monkeypatch.setattr(remote_exec, "build_remote_cmd_ws_url", lambda key, instance, **kw: selected.append(instance) or "wss://example.invalid")
    for selector in (views[0].label, views[0].handle):
        result = remote_exec.workload_exec(
            service, key=ref.key, workload=kind, rows=raw, instance=selector,
            command="true", timeout=1, on_output=None,
        )
        assert result.returncode == 0 and selected[-1] == views[0].handle


@pytest.mark.parametrize("entry", ["images", "notebooks"])
@pytest.mark.parametrize("form", ["name", "selector", "ref", "save_handle"])
def test_image_wait_ref_forms_and_workspace(client, monkeypatch, entry, form):
    from types import SimpleNamespace
    from inspire import Image, ImageRef, ImageSelector, ImageSaveHandle, NotebookRef, Resource, WorkspaceRef
    from inspire.platform.web import browser_api as api

    image_ref = ImageRef("image", client.account, client.base_url, "image-test", "ws-test")
    notebook_ref = NotebookRef("notebook", client.account, client.base_url, "nb-test", "ws-test")
    selection = {"name": "image", "selector": ImageSelector("image", "private"),
                 "ref": image_ref, "save_handle": ImageSaveHandle("image", image_ref, notebook_ref)}[form]
    monkeypatch.setattr(client.workspaces, "get", lambda name: Resource(str(name), WorkspaceRef(str(name), client.account, client.base_url, "other" if name == "other" else "ws-test", "ws-test")))
    monkeypatch.setattr(client.images, "get", lambda value, **kw: Image("image", image_ref, "private", "registry/image"))
    calls = []
    ready = SimpleNamespace(status="READY")
    monkeypatch.setattr(api, "wait_for_image_ready", lambda **kw: calls.append(kw) or ready)
    wait = client.images.wait_ready if entry == "images" else client.notebooks.wait_image_ready
    assert wait(selection, workspace="workspace", timeout=12, poll_interval=2) is ready
    assert calls[-1]["image_id"] == "image-test"
    assert calls[-1]["timeout"] == 12 and calls[-1]["poll_interval"] == 2
    if form in {"ref", "save_handle"}:
        assert wait(selection) is ready
        with pytest.raises(ValidationError, match="workspace"):
            wait(selection, workspace="other")
    else:
        with pytest.raises(ValidationError, match="workspace"):
            wait(selection)


@pytest.mark.parametrize("entry", ["images", "notebooks"])
def test_image_wait_unconfirmed_handle_fails_before_lookup(client, monkeypatch, entry):
    from inspire import ImageSaveHandle, NotebookRef
    from inspire.platform.web import browser_api as api
    handle = ImageSaveHandle("image", None, NotebookRef("nb", client.account, client.base_url, "nb", "ws-test"))
    monkeypatch.setattr(api, "wait_for_image_ready", lambda **kw: pytest.fail("no identity"))
    wait = client.images.wait_ready if entry == "images" else client.notebooks.wait_image_ready
    with pytest.raises(ValidationError, match="identity"):
        wait(handle)


@pytest.mark.parametrize("raw,expected", [
    ("tb_status_running", "RUNNING"), (" Tb_StAtUs_StOpPeD ", "STOPPED"),
    ("creating", "CREATING"), (" NewState ", "NEWSTATE"), ("", "UNKNOWN"),
    (None, "UNKNOWN"), ("https://internal.example/token", "UNKNOWN"),
    ("/private/path", "UNKNOWN"),
    ("12345678-1234-1234-1234-123456789abc", "UNKNOWN"),
])
def test_tensorboard_cli_and_sdk_share_public_status(client, raw, expected):
    from types import SimpleNamespace
    from inspire.cli.commands.tensorboard.tensorboard_commands import board_row, board_detail
    board = SimpleNamespace(
        name="board", tb_id="tb-test", status=raw, job_name="", job_id="",
        summary_path="", url="", project_name="", compute_group_name="",
        auto_stop_ms="", running_time_ms="", created_at="",
    )
    assert board_row(board)["status"] == expected
    assert board_detail(board)["status"] == expected
    assert client.tensorboards._board(board, "ws-test").status == expected


@pytest.mark.parametrize("plan_name", ["JobPlan", "NotebookPlan", "HPCJobPlan", "RayJobPlan", "ServingPlan"])
def test_plan_common_surface(plan_name):
    import inspire
    plan_type = getattr(inspire, plan_name)
    hints = get_type_hints(plan_type)
    assert hints["image"] is inspire.Image
    from typing import Any
    assert hints["create_kwargs"] == dict[str, Any]
    assert isinstance(inspect.getattr_static(plan_type, "summary"), property)
    assert callable(plan_type.to_dict)
