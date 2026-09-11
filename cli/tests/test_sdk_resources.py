"""Phase B facades: fake platform calls and compare shared CLI JSON contracts."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, asdict, replace
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from test_sdk import client as client
from inspire import (
    APIKeyRef,
    DatasetMount,
    WorkspaceRef,
    Resource,
    AmbiguousResourceError,
    ValidationError,
    ResolutionIncompleteError,
    SubmissionUncertainError,
    MutationUncertainError,
)
from inspire.platform.web import browser_api as api, plaza
from inspire.platform.web.browser_api import api_keys, datasets as mounts_api, schedule_config
from inspire.platform.web.browser_api.models import ModelInfo as PlatformModel
from inspire.platform.web.browser_api.projects import ProjectInfo as PlatformProject
from inspire.platform.web.browser_api.images import CustomImageInfo
from inspire.services.catalog import resource_usage


@pytest.fixture
def catalog(client, monkeypatch):
    from importlib import import_module
    from inspire.config import Config
    from inspire.platform.web import session as session_module
    from inspire.platform.web.browser_api import workspaces

    session = client._transport.session
    session.all_workspace_ids = ["ws-test"]
    session.all_workspace_names = {"ws-test": "Workspace"}
    ws = Resource(
        "Workspace",
        WorkspaceRef("Workspace", client.account, client.base_url, "ws-test", "ws-test"),
    )
    monkeypatch.setattr(
        workspaces,
        "try_enumerate_workspaces",
        lambda *a, **kw: [{"name": "Workspace", "id": "ws-test"}],
    )
    monkeypatch.setattr(session_module, "get_web_session", lambda **kw: session)
    for name in (
        "account.check",
        "account.permissions_cmd",
        "account.api_key",
        "model.model_commands",
        "resources.resources_usage",
        "resources.resources_list",
        "resources.resources_policy",
        "resources.resources_node_events",
    ):
        module = import_module("inspire.cli.commands." + name)
        monkeypatch.setattr(module, "get_web_session", lambda **kw: session)
    import socket

    monkeypatch.setattr(
        socket.socket, "connect", lambda *a, **kw: pytest.fail("unexpected network I/O")
    )
    monkeypatch.setattr(
        Config, "from_files_and_env", classmethod(lambda cls, **kw: (client._config, {}))
    )
    monkeypatch.setattr(api, "get_current_user", lambda **kw: {"id": "user-test", "name": "Ada"})
    return ws


def cli_json(*args):
    from inspire.cli.main import main

    result = CliRunner().invoke(main, ["--json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


def test_account_metadata_check_context_permissions(client, catalog, monkeypatch):
    from inspire.services.account import account_context

    calls = []

    def current(**kwargs):
        calls.append(kwargs)
        return {"id": "user-test", "name": "Ada", "password": "must-not-escape"}

    monkeypatch.setattr(api, "get_current_user", current)
    info = client.account_info.current()
    assert info.alias == cli_json("account", "current")["name"] == "alpha"
    assert info.user_name == "Ada"
    check = client.account_info.check()
    assert check.ok and calls[-1]["refresh"] is True
    assert "password" not in check.user
    assert cli_json("account", "check")["authenticated"] == check.ok
    assert "must-not-escape" not in repr(check)
    with pytest.raises(FrozenInstanceError):
        info.alias = "other"
    monkeypatch.setattr(
        api, "list_all_projects", lambda **kw: [PlatformProject("p", "Project", "ws-test")]
    )
    monkeypatch.setattr(api, "list_compute_groups", lambda **kw: [{"id": "g", "name": "GPU"}])
    context = client.account_info.context(limit=1)
    assert context.to_dict() == cli_json("account", "context", "--limit", "1")
    assert account_context.bound_context(context.to_dict(), None) == context.to_dict()
    monkeypatch.setattr(api, "get_user_permissions", lambda **kw: ["read", "read", "write"])
    permissions = client.account_info.permissions()
    assert [asdict(x) for x in permissions] == cli_json(
        "account", "permissions", "--workspace", "all"
    )["items"]
    assert [x.permission for x in client.account_info.permissions(catalog.ref)] == ["read", "write"]


def test_account_check_configuration_and_authentication_failures(client, catalog, monkeypatch):
    from inspire.config import Config

    bad = replace(client._config, base_url="https://api.example.com", password="")
    monkeypatch.setattr(Config, "from_files_and_env", classmethod(lambda cls, **kw: (bad, {})))
    monkeypatch.setattr(
        api, "get_current_user", lambda **kw: pytest.fail("invalid config dispatched")
    )
    check = client.account_info.check()
    assert not check.ok and len(check.issues) == 2 and check.user is None
    monkeypatch.setattr(
        Config, "from_files_and_env", classmethod(lambda cls, **kw: (client._config, {}))
    )

    def fail(**kw):
        raise ValueError("platform authentication refused")

    monkeypatch.setattr(api, "get_current_user", fail)
    assert client.account_info.check().issues == ("platform authentication refused",)


def test_api_keys_resolution_paging_and_single_dispatch(client, catalog, monkeypatch):
    keys = [api_keys.APIKeyInfo("key-a", "same", "1"), api_keys.APIKeyInfo("key-b", "SAME", "2")]
    monkeypatch.setattr(api_keys, "list_api_keys", lambda **kw: keys)
    page = client.api_keys.list(limit=1)
    assert page.total == 2 and page.next_cursor
    assert client.api_keys.list(limit=1, cursor=page.next_cursor).items[0].ref.key == "key-b"
    assert [x.to_dict() for x in client.api_keys.list().items] == cli_json(
        "account", "api-key", "list"
    )["items"]
    with pytest.raises(AmbiguousResourceError):
        client.api_keys.get("same")
    ref = page.items[0].ref
    assert client.api_keys.get(ref).name == "same"
    calls = []

    def write(*args, **kwargs):
        calls.append((args, client._transport._write.copy()))

    monkeypatch.setattr(api_keys, "create_api_key", write)
    monkeypatch.setattr(api_keys, "delete_api_key", write)
    monkeypatch.setattr(api_keys, "list_api_keys", lambda **kw: pytest.fail("post-write list"))
    created = client.api_keys.create("new")
    assert created.name == "new" and created.ref is None
    assert client.api_keys.delete(ref) is None
    assert len(calls) == 2 and calls[0][1]["create"] and not calls[1][1]["create"]
    monkeypatch.setattr(api_keys, "get_api_key_plaintext", lambda *a, **kw: "explicit-secret")
    assert client.api_keys.plaintext(ref) == "explicit-secret"
    assert "explicit-secret" not in repr(created)
    with pytest.raises(ValidationError):
        client.api_keys.plaintext(replace(ref, account="beta"))


@pytest.mark.parametrize("create", [True, False])
def test_api_keys_transport_failure_is_uncertain_and_never_replayed(
    client, catalog, monkeypatch, create
):
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        client._transport._write["sent"] = True
        raise ValueError("response lost")

    monkeypatch.setattr(client._transport, "_once", fail)
    with pytest.raises(SubmissionUncertainError if create else MutationUncertainError):
        if create:
            client.api_keys.create("new")
        else:
            client.api_keys.delete(APIKeyRef("key", client.account, client.base_url, "key-a"))
    assert len(calls) == 1


def test_projects_global_detail_budget_and_owners(client, catalog, monkeypatch):
    project = PlatformProject(
        project_id="p",
        name="Project",
        workspace_id="ws-test",
        member_remain_budget="10",
        remain_budget="100",
    )
    calls = []
    monkeypatch.setattr(api, "list_all_projects", lambda **kw: calls.append("global") or [project])
    monkeypatch.setattr(
        api, "list_projects", lambda **kw: calls.append(kw["workspace_id"]) or [project]
    )
    result = client.projects.list()
    assert calls == ["global"]
    assert result.items[0].to_dict() == cli_json("project", "list")["items"][0]
    assert client.projects.get("PROJECT", workspace=catalog.ref).ref.workspace_id == "ws-test"
    detail = {
        "name": "Project",
        "en_name": "project-en",
        "budget": "100",
        "creator": {"name": "Ada"},
    }
    usage = {"used": "1,234", "train": "1,200", "inference": "34"}
    monkeypatch.setattr(api, "get_project_detail", lambda *a, **kw: detail)
    monkeypatch.setattr(api, "get_project_budget_usage", lambda *a, **kw: usage)
    from inspire.cli.commands.project import project_commands

    monkeypatch.setattr(project_commands, "_resolve_project_name", lambda *a, **kw: "p")
    sdk_detail = client.projects.detail(result.items[0].ref)
    assert sdk_detail.spent_budget == "1234"
    assert sdk_detail.to_dict() == cli_json("project", "detail", "Project")
    monkeypatch.setattr(api, "list_project_owners", lambda **kw: [{"id": "owner", "name": "Ada"}])
    owners = client.projects.owners()
    assert owners[0].ref.key == "owner"
    assert [x.to_dict() for x in owners] == cli_json("project", "owners")["items"]


def test_image_catalog_sources_keyword_dedupe_detail(client, catalog, monkeypatch):
    image = CustomImageInfo(
        "image-a",
        "registry/image",
        "Image",
        "torch",
        "v1",
        "SOURCE_PUBLIC",
        "READY",
        "",
        "",
        "VISIBILITY_PRIVATE",
    )
    calls = []

    def listing(source, **kw):
        calls.append(source)
        return [image]

    monkeypatch.setattr(api, "list_images_by_source", listing)
    page = client.images.list(catalog.ref, keyword="IMAGE:v1")
    assert calls == ["official", "public", "project", "private"]
    assert page.total == 1
    assert [x.to_dict() for x in page.items] == cli_json(
        "image", "list", "--workspace", "Workspace", "--source", "all", "--keyword", "IMAGE:v1"
    )["items"]
    monkeypatch.setattr(api, "get_image_detail", lambda **kw: image)
    from inspire.cli.commands.image import image_commands

    monkeypatch.setattr(image_commands, "_resolve_image_name", lambda *a, **kw: "image-a")
    assert client.images.detail(page.items[0].ref, workspace=catalog.ref).to_dict() == cli_json(
        "image", "detail", "Image:v1", "--workspace", "Workspace"
    )


def test_datasets_pagination_detail_tags_and_validation(client, catalog, monkeypatch):
    summaries = [plaza.DatasetSummary(code=f"data-{i}", dataset_id=i + 1) for i in range(3)]
    requests = []

    def listing(**kw):
        requests.append(kw)
        return (summaries[:2], 3) if kw["page"] == 1 else (summaries[2:], 3)

    monkeypatch.setattr(plaza, "resolve_tag_ids", lambda tags, **kw: [7] if tags else [])
    monkeypatch.setattr(plaza, "list_datasets", listing)
    page = client.datasets.list(keyword="data", tag="tag", limit=2)
    assert [x.name for x in page.items] == ["data-0", "data-1"]
    assert (
        client.datasets.list(keyword="data", tag="tag", limit=2, cursor=page.next_cursor)
        .items[0]
        .name
        == "data-2"
    )
    assert requests[0]["keyword"] == "data" and requests[0]["tag_ids"] == [7]
    with pytest.raises(ValidationError):
        client.datasets.list(keyword="different", tag="tag", cursor=page.next_cursor)
    monkeypatch.setattr(plaza, "list_datasets", lambda **kw: (summaries, 3))
    assert [x.to_dict() for x in client.datasets.list().items] == cli_json("dataset", "list")[
        "items"
    ]
    detail = plaza.DatasetDetail(
        code="data-0", dataset_id=1, versions=[plaza.DatasetVersion(code="v1", version_id=3)]
    )
    monkeypatch.setattr(plaza, "resolve_dataset_by_code", lambda *a, **kw: summaries[0])
    monkeypatch.setattr(plaza, "get_dataset_detail", lambda *a, **kw: detail)
    assert client.datasets.get("data-0").to_dict() == cli_json("dataset", "show", "data-0")
    monkeypatch.setattr(
        plaza, "list_dataset_tags", lambda **kw: [plaza.DatasetTag("tag", "video", 7)]
    )
    assert [x.to_dict() for x in client.datasets.tags()] == cli_json("dataset", "tags")["items"]
    verdicts = [
        mounts_api.DatasetValidation("data-0", "v1", True),
        mounts_api.DatasetValidation("data-1", "v2", False, error="平台：无权限"),
    ]

    def validate(mounts, **kw):
        assert mounts == [DatasetMount("data-0", "v1"), DatasetMount("data-1", "v2")]
        return verdicts

    monkeypatch.setattr(mounts_api, "validate_dataset_mounts", validate)
    sdk = client.datasets.validate(["data-0:v1", DatasetMount("data-1", "v2")], workspace=catalog.ref)
    assert sdk[1].reason == "平台：无权限" and not sdk[1].mountable
    from inspire.cli.commands.dataset import dataset_commands

    monkeypatch.setattr(dataset_commands, "validate_dataset_mounts", validate)
    from inspire.cli.main import main

    result = CliRunner().invoke(
        main,
        ["--json", "dataset", "validate", "data-0:v1", "data-1:v2", "--workspace", "Workspace"],
    )
    assert result.exit_code != 0
    assert [x.to_dict() for x in sdk] == json.loads(result.output)["data"]["items"]
    with pytest.raises(ValidationError, match="expects"):
        client.datasets.validate(["bad"], workspace=catalog.ref)


def test_dataset_applications_modes(client, catalog, monkeypatch):
    application = plaza.DatasetApplication(
        "data", state="pending", applicant="Ada", application_id=1
    )
    monkeypatch.setattr(plaza, "list_dataset_applications", lambda **kw: ([application], 1))
    monkeypatch.setattr(plaza, "list_dataset_approvals", lambda **kw: ([application], 1))
    monkeypatch.setattr(plaza, "find_dataset_applications", lambda *a, **kw: [application])
    for incoming in (False, True):
        flags = ("--to-approve",) if incoming else ()
        assert [
            x.to_dict() for x in client.datasets.applications(to_approve=incoming).items
        ] == cli_json("dataset", "applications", *flags)["items"]
        assert [
            x.to_dict() for x in client.datasets.applications("data", to_approve=incoming).items
        ] == cli_json("dataset", "applications", "data", *flags)["items"]


def test_model_status_versions_deploy_config_and_json(client, catalog, monkeypatch):
    model = PlatformModel("m", "Model", latest_version="2", status="2")
    monkeypatch.setattr(api, "list_models", lambda **kw: ([model], 1))
    from inspire.cli.commands.model import model_commands

    monkeypatch.setattr(model_commands, "_resolve_model_name", lambda *a, **kw: "m")
    data = {
        "model": {"name": "Model", "version": 1, "description": "weights", "has_published": True}
    }
    records = {
        "list": [
            {
                "model": {"version": 2, "status": 2, "model_size_gi": 4},
                "running_infrence_serving": 1,
            },
            {"model": {"version": 1, "status": 2}, "running_infrence_serving": 2},
        ]
    }
    monkeypatch.setattr(api, "get_model_detail", lambda *a, **kw: data)
    monkeypatch.setattr(api, "list_model_version_records", lambda *a, **kw: records)
    monkeypatch.setattr(api, "get_model_vllm_compatibility", lambda *a, **kw: {2: True})
    pending_calls = []
    monkeypatch.setattr(
        api,
        "check_model_inference_serving_pending",
        lambda **kw: pending_calls.append(kw) or {"has_pending_serving": True},
    )
    monkeypatch.setattr(
        api,
        "list_model_inference_servings",
        lambda **kw: ([{"name": "live", "status": 4}, {"name": "failed", "status": 3}], 2),
    )
    assert [x.to_dict() for x in client.models.list(catalog.ref).items] == cli_json(
        "model", "list", "--workspace", "Workspace"
    )["items"]
    snapshot = client.models.get("MODEL", workspace=catalog.ref)
    assert client.models.status([]) == ()
    assert client.models.status([snapshot.ref, "MODEL"], workspace=catalog.ref) == (snapshot, snapshot)
    status = client.models.detail(snapshot.ref, workspace=catalog.ref)
    assert status.version == "V2" and status.vllm_ready is True and status.pending_serving
    assert status.servings == [{"name": "live", "status": "RUNNING"}]
    assert status.other_versions_in_use == ["V1"] and "version" not in pending_calls[0]
    assert status.to_dict() == cli_json("model", "status", "Model", "--workspace", "Workspace")
    assert [x.to_dict() for x in client.models.versions(status.ref, workspace=catalog.ref)] == cli_json(
        "model", "versions", "Model", "--workspace", "Workspace"
    )["items"]
    monkeypatch.setattr(
        api,
        "get_model_recommended_config",
        lambda *a, **kw: {
            "min_node_count": "1",
            "min_gpu_count_per_node": "2",
            "min_cpu_count_per_node": "20",
            "min_memory_size_gib_per_node": "200",
        },
    )
    monkeypatch.setattr(api, "check_model_vllm_compatible", lambda *a, **kw: True)
    assert client.models.deploy_config(status.ref, workspace=catalog.ref).to_dict() == cli_json(
        "model", "deploy-config", "Model", "--workspace", "Workspace"
    )


def test_model_exact_resolution_pagination_and_ref_scope(client, catalog, monkeypatch):
    models = [PlatformModel("one", "same"), PlatformModel("two", "SAME")]
    monkeypatch.setattr(api, "list_models", lambda **kw: ([models[kw["page"] - 1]], 2))
    page = client.models.list(catalog.ref, limit=1)
    assert (
        client.models.list(catalog.ref, limit=1, cursor=page.next_cursor).items[0].ref.key == "two"
    )
    with pytest.raises(AmbiguousResourceError):
        client.models.get("same", workspace=catalog.ref)
    assert client.models.get(page.items[0].ref, workspace=catalog.ref).name == "same"
    with pytest.raises(ValidationError):
        client.models.status([replace(page.items[0].ref, account="other")], workspace=catalog.ref)[0]
    monkeypatch.setattr(api, "list_models", lambda **kw: ([models[0]], 2))
    with pytest.raises(ResolutionIncompleteError):
        client.models.get("same", workspace=catalog.ref)


@pytest.mark.parametrize("details,mine", [(False, False), (True, False), (False, True)])
def test_resources_usage_sections_match_cli(client, catalog, monkeypatch, details, mine):
    from test_resources_usage import _task

    tasks = [_task(name="train", user="Ada", project="Vision", gpus=8, nodes=("node-a",))]
    monkeypatch.setattr(api, "list_task_usage", lambda *a, **kw: tasks)
    monkeypatch.setattr(resource_usage, "is_fair_scheduling_workspace", lambda *a: False)
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.workspaces.is_fair_scheduling_workspace", lambda *a: False
    )
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.availability.list_compute_groups",
        lambda **kw: [{"id": "group", "name": "H200"}],
    )
    monkeypatch.setattr(api, "list_compute_groups", lambda **kw: [{"id": "group", "name": "H200"}])
    monkeypatch.setattr(
        api,
        "list_member_usage",
        lambda *a, **kw: [
            SimpleNamespace(
                project_name="Vision",
                gpus=8,
                cpus=160.0,
                memory_gib=800.0,
                gpu_nodes=1,
                cpu_nodes=0,
                hpc_nodes=0,
            )
        ],
    )
    kwargs = {"project": "Vis", "mine": mine, "details": details}
    args = ["resources", "usage", "--workspace", "Workspace", "--project", "Vis"]
    if mine:
        args += ["--mine"]
    else:
        kwargs["group"] = "H200"
        args += ["--group", "H200"]
        if details:
            args += ["--details"]
    usage = client.resources.usage(catalog.ref, **kwargs)
    assert usage.to_dict() == cli_json(*args)
    assert usage.filters == {"project": "Vis"}
    if not mine:
        assert usage.compute_groups == ["H200"]
    with pytest.raises(ValidationError, match="pre-aggregated"):
        client.resources.usage(catalog.ref, mine=True, details=True)


def test_resources_policy_availability_node_events(client, catalog, monkeypatch):
    from inspire.cli.commands.resources.resources_policy import _public_row

    policies = [
        schedule_config.WorkloadSchedulePolicy("job", auto_reclaim=False, max_runtime_minutes=120)
    ]
    monkeypatch.setattr(schedule_config, "get_workspace_schedule_policy", lambda *a, **kw: policies)
    from inspire.cli.commands.resources import resources_policy

    monkeypatch.setattr(
        resources_policy, "get_workspace_schedule_policy", lambda *a, **kw: policies
    )
    assert [
        _public_row(x, workspace="Workspace") for x in client.resources.policy(catalog.ref)
    ] == cli_json("resources", "policy", "--workspace", "Workspace")["items"]
    cpu = SimpleNamespace(
        resource_kind="cpu",
        workspace_name="Workspace",
        group_name="CPU",
        cpu_total=100.0,
        cpu_used=30.0,
        cpu_available=70.0,
        memory_total_gib=200.0,
        memory_used_gib=60.0,
        memory_available_gib=140.0,
    )
    monkeypatch.setattr(api, "get_resource_inventory", lambda **kw: [cpu])
    assert [
        x.to_dict() for x in client.resources.availability(catalog.ref, include_cpu=True)
    ] == cli_json("resources", "availability", "--workspace", "Workspace", "--include-cpu")["items"]
    events = [
        {"last_timestamp": "10", "reason": "Ready", "event_type": "Normal"},
        {"last_timestamp": "20", "reason": "OOM", "event_type": "Warning"},
    ]
    monkeypatch.setattr(api, "list_node_events", lambda *a, **kw: events)
    assert client.resources.node_events(["node"], since=15, type="warning").items == (events[1],)
    assert client.resources.node_events("node", limit=1).truncated
    from inspire.cli.utils.events import public_event

    assert [public_event(x) for x in client.resources.node_events("node").items] == cli_json(
        "resources", "node-events", "node"
    )["items"]


def test_all_workspaces_fanout_and_model_project_scope(client, catalog, monkeypatch):
    other = Resource(
        "Other", replace(catalog.ref, name="Other", key="ws-other", workspace_id="ws-other")
    )
    monkeypatch.setattr(client.workspaces, "_all", lambda: [catalog, other])
    permission_calls = []
    monkeypatch.setattr(
        api,
        "get_user_permissions",
        lambda **kw: permission_calls.append(kw["workspace_id"]) or ["read"],
    )
    assert len(client.account_info.permissions("ALL")) == 2
    assert permission_calls == ["ws-test", "ws-other"]
    monkeypatch.setattr(
        api,
        "list_projects",
        lambda **kw: (
            [PlatformProject(project_id="project", name="Project", workspace_id=kw["workspace_id"])]
            if kw["workspace_id"] == "ws-other"
            else []
        ),
    )
    calls = []

    def models(**kwargs):
        calls.append(kwargs)
        return [PlatformModel("m", "Model")], 1

    monkeypatch.setattr(api, "list_models", models)
    result = client.models.list("ALL", project="Project", keyword="Model")
    assert result.total == 1 and result.items[0].ref.workspace_id == "ws-other"
    assert calls[0]["project_ids"] == ["project"] and calls[0]["keyword"] == "Model"
    assert calls[0]["user_id"] == "user-test"


def test_application_detail_cursor_and_millisecond_events(client, catalog, monkeypatch):
    rows = [plaza.DatasetApplication("data", application_id=i + 1) for i in range(3)]
    monkeypatch.setattr(plaza, "find_dataset_applications", lambda *a, **kw: rows[: kw["limit"]])
    first = client.datasets.applications("data", limit=2)
    second = client.datasets.applications("data", limit=2, cursor=first.next_cursor)
    assert len(first.items) == 2 and second.items[0].ref.key == "3"
    assert second.next_cursor is None and first.total is None
    events = [{"last_timestamp": "1700000000000"}, {"last_timestamp": "1700000020000"}]
    monkeypatch.setattr(api, "list_node_events", lambda *a, **kw: events)
    assert client.resources.node_events("node", since=1700000010).items == (events[1],)


def test_collect_pages_deduplicates_overlapping_pages(client):
    pages = {1: (["a", "b"], 3), 2: (["b", "c"], 3)}
    calls = []

    def fetch(*, page, page_size):
        calls.append(page)
        return pages[page]

    assert client.datasets._collect_pages(fetch, str) == ["a", "b", "c"]
    assert calls == [1, 2]


@pytest.mark.parametrize("empty", [False, True])
def test_collect_pages_rejects_incomplete_catalog(client, empty):
    calls = []

    def fetch(*, page, page_size):
        calls.append(page)
        return ([] if empty else ["same"], 101)

    with pytest.raises(ResolutionIncompleteError):
        client.datasets._collect_pages(fetch, str)
    assert len(calls) == (1 if empty else 100)


@pytest.mark.parametrize("kind", ["job", "image", "model", "dataset", "future_kind"])
def test_unscoped_refs_accept_any_workspace(client, kind):
    from inspire.sdk.models import ResourceRef

    ref_type = type("ExampleRef", (ResourceRef,), {"kind": kind})
    ref = ref_type(
        account=client.account, base_url=client.base_url,
        name="Resource", key="resource", workspace_id="",
    )
    client._validate_ref(ref, ref_type, "workspace")
    with pytest.raises(ValidationError):
        client._validate_ref(replace(ref, workspace_id="other"), ref_type, "workspace")
    with pytest.raises(ValidationError):
        client._validate_ref(replace(ref, account="other"), ref_type, "workspace")
    with pytest.raises(ValidationError):
        client._validate_ref(replace(ref, base_url="https://other.invalid"), ref_type, "workspace")
    with pytest.raises(ValidationError):
        client._validate_ref(ref, WorkspaceRef, "workspace")


def test_dataset_validation_parses_strings_once_and_preserves_mounts(client, catalog, monkeypatch):
    from inspire.services.catalog import datasets

    calls = []
    original = datasets.parse_dataset_spec
    mount = DatasetMount("data-1", "v2")

    def parse(spec, **kwargs):
        calls.append(spec)
        return original(spec, **kwargs)

    def validate(mounts, **kwargs):
        assert mounts[0] == DatasetMount("data-0", "v1")
        assert mounts[1] is mount
        return []

    monkeypatch.setattr(datasets, "parse_dataset_spec", parse)
    monkeypatch.setattr(mounts_api, "validate_dataset_mounts", validate)
    assert client.datasets.validate([" data-0 : v1 ", mount], workspace=catalog.ref) == ()
    assert calls == [" data-0 : v1 "]
    with pytest.raises(ValidationError, match="specs data-1:v2 was given more than once"):
        client.datasets.validate([mount, " data-1 : v2 "], workspace=catalog.ref)


@pytest.mark.parametrize("include_name", [True, False])
def test_training_job_detail_matches_cli_json(client, catalog, monkeypatch, include_name):
    from inspire import JobRef
    from inspire.cli.commands.job import job_commands as cli
    from inspire.platform.web.browser_api import jobs

    ref = JobRef("Training", client.account, client.base_url, "job-test", catalog.ref.key)
    detail = {
        "job_id": ref.key,
        "workspace_id": catalog.ref.key,
        "status": "job_running",
        "project_name": "Project",
        "logic_compute_group_name": "Group",
        "framework_config": [{
            "cpu": 8, "memory_size": 32, "gpu_count": 1, "instance_count": 2,
            "image": "registry.example.test/training:v1",
        }],
        "priority": 6,
        "created_at": "1700000000000",
        "node_infos": [{"node_name": "worker-a"}],
        "command": "python train.py",
    }
    if include_name:
        detail["name"] = ref.name
    calls = []

    def fake_detail(key, **kwargs):
        calls.append(key)
        return detail

    monkeypatch.setattr(jobs, "get_job_detail_v2", fake_detail)
    monkeypatch.setattr(api, "get_job_detail_v2", fake_detail)
    monkeypatch.setattr(
        cli, "_run_readonly_web_job_operation",
        lambda **kw: kw["operation"](ref.key, client._transport.session),
    )
    monkeypatch.setattr(cli, "_close_web_client", lambda: None)
    job = client.jobs.get(ref)
    assert job.view == cli_json("job", "status", ref.name, "--workspace", catalog.name)
    assert calls == [ref.key, ref.key]
    assert job.view["name"] == ref.name
    assert job.view["compute_group"] == "Group"
    assert job.view["resource"]["nodes"] == 2
    assert job.view["priority"] == 6
    assert job.raw == detail and job.raw is not detail
    assert job.raw["command"] == "python train.py"
    assert "command" not in job.view and "framework_config" not in job.view
    assert job.to_dict() == job.view and job.to_dict() is not job.view


@pytest.mark.parametrize("nested_raw", [False, True])
def test_training_job_list_and_iter_views(client, catalog, monkeypatch, nested_raw):
    from dataclasses import dataclass, field
    from typing import Any
    from inspire.platform.web.browser_api import jobs

    @dataclass
    class JobWithRaw(jobs.JobInfo):
        raw: dict[str, Any] = field(default_factory=dict)

    model = JobWithRaw if nested_raw else jobs.JobInfo
    row = model.from_api_response({
        "job_id": "job-test", "name": "Training", "status": "job_running",
        "project_name": "Project", "logic_compute_group_name": "Group",
        "framework_config": [{"cpu": 8, "instance_count": 2}],
    })
    if nested_raw:
        row.raw = {"name": "stale-name", "image": "training:v1", "priority_level": "high"}
    monkeypatch.setattr(jobs, "list_jobs", lambda **kw: ([row], 1))
    for job in (*client.jobs.list(catalog.ref).items, *client.jobs.iter(catalog.ref)):
        assert job.view["name"] == "Training"
        assert job.view["compute_group"] == "Group"
        assert job.view["resource"]["cpu"] == 8
        assert job.to_dict() == job.view
        assert "raw" not in job.raw
        if nested_raw:
            assert job.raw["image"] == "training:v1"
            assert job.view["priority_level"] == "high"
