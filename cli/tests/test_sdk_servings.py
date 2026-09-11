"""Serving payload equivalence and single-dispatch lifecycle contracts."""

from __future__ import annotations
from dataclasses import replace
from types import SimpleNamespace
import json
import pytest
import requests
from click.testing import CliRunner
from test_sdk import client as client
from test_sdk_hpc import catalog as base_catalog  # noqa: F401
from inspire import ServingCreateSpec, ServingRef, ServingFailedError, ValidationError
from inspire import SubmissionUncertainError, MutationUncertainError, AmbiguousResourceError
from inspire.platform.web import browser_api as api
from inspire.platform.web.browser_api.servings import ServingInfo
from inspire.platform.web.browser_api.images import CustomImageInfo


@pytest.fixture
def catalog(client, base_catalog, monkeypatch):  # noqa: F811
    monkeypatch.setattr(api, "get_quota_priority_levels", lambda **kw: {})
    c = base_catalog
    c.group["support_job_type_list"] = '["inference_serving_customize", "tensorboard"]'
    monkeypatch.setattr(
        api,
        "list_serving_user_project",
        lambda **kw: {
            "projects": [{"project_id": c.project.project_id, "project_name": "Project"}]
        },
    )
    monkeypatch.setattr(api, "get_current_user", lambda **kw: {"id": "user-test"})
    monkeypatch.setattr(
        api,
        "list_models",
        lambda **kw: (
            [
                SimpleNamespace(
                    name="Model",
                    model_id="model-test",
                    status="READY",
                    created_at="",
                    latest_version="3",
                )
            ],
            1,
        ),
    )
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
    c.serving = ServingCreateSpec(
        "example",
        "Model",
        "python serve.py",
        8000,
        "Workspace",
        "Project",
        "Group",
        "0,8,32",
        "Image",
    )
    return c


def test_payload_equivalent_to_cli_dry_run(client, catalog, monkeypatch):
    from inspire.cli.commands.serving import serving_commands as cli
    from inspire.cli.utils import quota_resolver
    from inspire.cli.main import main

    spec = replace(
        catalog.serving,
        model_version=2,
        replicas=3,
        nodes_per_replica=2,
        shm_gib=8,
        priority=6,
        custom_domain="test-domain",
        description="description",
        auto_scaling=True,
        public_path_readonly=False,
    )
    plan = client.servings.plan(spec)
    monkeypatch.setattr(cli, "get_web_session", lambda: client._transport._session)
    monkeypatch.setattr(cli, "select_workspace_id", lambda **kw: "ws-test")
    monkeypatch.setattr(cli, "workspace_label", lambda *a: "Workspace")
    monkeypatch.setattr(cli, "_resolve_project_id", lambda **kw: "project-test")
    monkeypatch.setattr(cli, "resolve_by_name", lambda *a, **kw: "model-test")
    monkeypatch.setattr(quota_resolver, "resolve_quota", lambda **kw: catalog.resolved)
    captured = []
    monkeypatch.setattr(
        api, "create_serving", lambda **kw: captured.append(kw) or {"id": "serving-test"}
    )
    args = [
        "--json",
        "serving",
        "create",
        "--name",
        "example",
        "--model",
        "Model",
        "--model-version",
        "2",
        "--command",
        "python serve.py",
        "--port",
        "8000",
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
        "--replicas",
        "3",
        "--nodes-per-replica",
        "2",
        "--shm-size",
        "8",
        "--priority",
        "6",
        "--custom-domain",
        "test-domain",
        "--description",
        "description",
        "--auto-scaling",
        "--no-public-path-readonly",
    ]
    result = CliRunner().invoke(main, [*args, "--dry-run"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"] == plan.to_dict()
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert {k: v for k, v in captured[0].items() if k != "session"} == plan.create_kwargs
    assert plan.create_kwargs["resource_spec_price"]["cpu_type"] == "cpu"


@pytest.mark.parametrize("action", ["create", "start", "stop", "delete", "scale", "rollback"])
@pytest.mark.parametrize("outcome", ["ok", "timeout", "platform"])
def test_single_dispatch(client, catalog, monkeypatch, action, outcome):
    ref = ServingRef("example", client.account, client.base_url, "serving-test", "ws-test")
    plan = client.servings.plan(catalog.serving)
    monkeypatch.setattr(client.servings, "plan", lambda spec: plan)
    calls = []

    def once(method, path, body, *a, **kw):
        calls.append((path, {"body": body}))
        assert client._transport._write is not None
        client._transport._write["sent"] = True
        if outcome == "timeout":
            raise requests.ReadTimeout("lost response")
        if outcome == "platform":
            return {
                "ResponseMetadata": {
                    "Error": {"Code": "InvalidParameter", "Message": "平台原始错误"}
                }
            }
        return {"Result": {"inference_serving_id": "serving-test"}}

    monkeypatch.setattr(client._transport, "_once", once)
    monkeypatch.setattr(client._transport, "_refresh", lambda: pytest.fail("refreshed write"))

    def invoke():
        if action == "create":
            return client.servings.create(catalog.serving, operation_id="diagnostic")
        return getattr(client.servings, action)(
            ref, **({"replicas": 2} if action == "scale" else {"version": 2} if action == "rollback" else {})
        )

    if outcome == "ok":
        invoke()
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
    expected = {
        "create": "CreateServingConsole",
        "start": "StartServing",
        "stop": "StopServing",
        "delete": "DeleteServing",
        "scale": "ScaleServing",
        "rollback": "RollbackServing",
    }[action]
    assert calls[0][0].endswith("Action=" + expected)
    if action == "scale":
        assert calls[0][1]["body"]["replica"] == 2


def test_endpoint_and_traffic(client, monkeypatch):
    ref = ServingRef("example", client.account, client.base_url, "serving-test", "ws-test")
    monkeypatch.setattr(
        client.servings, "_binding",
        replace(client.servings._binding, get_detail=lambda *a, **kw: {
            "name": "example",
            "status": "RUNNING",
            "inference_serving_type": "EXCLUSIVE",
            "extra_info": {"service": "https://serving.example/"},
        }),
    )
    info = client.servings.api(ref, affinity_key="session-1")
    assert info.endpoint == "https://serving.example"
    assert info.base_url == "https://serving.example/v1"
    assert "session-1" in info.example
    calls = []

    def metrics(*a, **kw):
        calls.append(kw)
        return [
            {
                "metric_type": "QPS",
                "time_series": [{"data": "2"}, {"data": "4"}, {"data": "invalid"}],
            }
        ]

    monkeypatch.setattr(api, "get_serving_api_metrics", metrics)
    result = client.servings.api_metrics(ref, metric="qps", window="30m")
    assert result.series[0].to_dict() == dict(metric="QPS", count=2, min=2, max=4, avg=3, last=4, total=6)
    assert calls[0]["end_timestamp"] - calls[0]["start_timestamp"] == 1800


def test_list_get_wait_and_missing_identity(client, catalog, monkeypatch):
    monkeypatch.setattr(
        api,
        "list_servings",
        lambda **kw: (
            [ServingInfo("s1", "same", "running"), ServingInfo("s2", "SAME", "stopped")],
            2,
        ),
    )
    assert len(client.servings.list("Workspace", status="running").items) == 1
    with pytest.raises(AmbiguousResourceError):
        client.servings.get("same", workspace="Workspace")
    ref = ServingRef("same", client.account, client.base_url, "s1", "ws-test")
    monkeypatch.setattr(
        client.servings, "_binding",
        replace(client.servings._binding, get_detail=lambda *a, **kw: {"name": "same", "status": "FAILED"}),
    )
    rows = client.servings.status([ref])
    assert isinstance(rows, tuple) and rows[0].status == "FAILED"
    assert client.servings.status([]) == ()
    with pytest.raises(ServingFailedError):
        client.servings.wait(ref, raise_on_failure=True)
    monkeypatch.setattr(api, "create_serving", lambda **kw: {})
    with pytest.raises(SubmissionUncertainError, match="inspect servings"):
        client.servings.create(catalog.serving)


def test_events_logs_instances_and_follow(client, monkeypatch):
    ref = ServingRef("example", client.account, client.base_url, "s1", "ws-test")
    monkeypatch.setattr(
        api,
        "list_serving_instances",
        lambda *a, **kw: (
            [{"name": "project/sv-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa-0", "rank": 0}],
            1,
        ),
    )
    assert client.servings.instances(ref)[0].label == "rank=0"
    monkeypatch.setattr(
        api,
        "list_serving_events",
        lambda *a, **kw: [
            {
                "type": "Warning",
                "reason": "Scheduling",
                "object_id": kw.get("pod_names", [""])[0],
                "message": "pending",
            }
        ],
    )
    assert len(client.servings.events(ref, instance="0", reason="sched").items) == 1
    gen = client.servings.follow_events(ref, interval=0.001, instance="0")
    assert next(gen).items
    gen.close()

    def logs(**kw):
        assert kw["inference_serving_id"] == "s1"
        assert kw["pod_names"] == ["project/sv-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa-0"]
        return [{"timestamp": 1, "message": "first"}, {"timestamp": 2, "message": "last"}], 3

    monkeypatch.setattr(api, "list_serving_logs", logs)
    result = client.servings.logs(ref, instance="0", window="30m", tail=1)
    assert result.items[-1]["message"] == "last" and result.truncated


def test_scale_zero_and_quota_priority(client, catalog, monkeypatch):
    from inspire import ValidationError

    ref = ServingRef("example", client.account, client.base_url, "s1", "ws-test")
    calls = []
    monkeypatch.setattr(api, "scale_serving", lambda *a, **kw: calls.append(kw))
    client.servings.scale(ref, replicas=0)
    assert calls[0]["replica"] == 0
    monkeypatch.setattr(api, "get_quota_priority_levels", lambda **kw: {"quota-test": ("low",)})
    with pytest.raises(ValidationError, match="published as LOW-priority only"):
        client.servings.plan(replace(catalog.serving, priority=10))


def test_quotas_metrics_configs_and_history(client, catalog, monkeypatch):
    from inspire.platform.web.browser_api import metrics as metric_api

    ref = ServingRef("example", client.account, client.base_url, "s1", "ws-test")
    price_calls = []
    monkeypatch.setattr(
        api, "get_resource_prices", lambda **kw: price_calls.append(kw) or [catalog.price]
    )
    assert client.servings.quotas("Workspace").items[0].quota.cpu == 8
    assert price_calls[0]["schedule_config_type"] == "SCHEDULE_CONFIG_TYPE_SERVE"
    monkeypatch.setattr(
        client.servings, "_binding",
        replace(client.servings._binding, get_detail=lambda *a, **kw: {"status": "RUNNING", "logic_compute_group_id": "group-test"}),
    )
    calls = []
    monkeypatch.setattr(
        metric_api, "get_resource_metrics_by_time", lambda **kw: calls.append(kw) or []
    )
    assert client.servings.metrics(ref, metric="cpu", window="30m") == ()
    assert calls[0]["task_type"] == "inference_serving"
    assert calls[0]["logic_compute_group_id"] == "group-test"
    monkeypatch.setattr(api, "get_serving_configs", lambda **kw: {})
    assert client.servings.configs("Workspace").to_dict() == {"items": []}
    monkeypatch.setattr(
        api,
        "list_serving_versions",
        lambda *a, **kw: ([{"version": 2, "command": "python serve.py"}], 1),
    )
    assert client.servings.versions(ref)[0].version == 2
    monkeypatch.setattr(
        api,
        "list_serving_scale_history",
        lambda *a, **kw: ([{"id": "h1", "replicas_before_scale": 1, "replicas_after_scale": 2}], 1),
    )
    assert client.servings.scale_history(ref).items[0].replicas_to == 2


@pytest.mark.parametrize("via_binding", [False, True])
def test_instances_expand_to_reported_total(client, monkeypatch, via_binding):
    ref = ServingRef("example", client.account, client.base_url, "s1", "ws-test")
    rows = [{"name": f"project/pod-{rank}", "rank": rank} for rank in range(201)]
    calls = []

    def fetch(key, *, page, page_size, session):
        assert key == ref.key
        calls.append((page, page_size))
        return rows[:page_size], len(rows)

    monkeypatch.setattr(api, "list_serving_instances", fetch)
    if via_binding:
        result, total = client.servings._binding.fetch_instances(
            ref.key, session=client._transport.session
        )
        assert total == 201
    else:
        result = client.servings.instances(ref)
    assert len(result) == 201
    assert calls == [(1, 200), (1, 201)]


def test_binding_adapts_serving_paging(client, monkeypatch):
    calls = []

    def fetch(**kwargs):
        calls.append(kwargs)
        return [], 0

    monkeypatch.setattr(api, "list_servings", fetch)
    assert client.servings._binding.list_jobs(
        workspace_id="ws-test", page_num=2, page_size=50, session=None
    ) == ([], 0)
    assert calls == [dict(workspace_id="ws-test", page=2, page_size=50, session=None)]


@pytest.mark.parametrize("incomplete", [False, True])
def test_list_pages_without_expansion(client, catalog, monkeypatch, incomplete):
    from inspire import ResolutionIncompleteError

    rows = [ServingInfo(f"s{i}", f"service-{i}", "running") for i in range(200)]
    calls = []

    def fetch(**kwargs):
        page, size = kwargs["page"], kwargs["page_size"]
        assert size == 20
        calls.append((page, size))
        return (rows[:size] if incomplete else rows[(page - 1) * size:page * size]), len(rows)

    monkeypatch.setattr(api, "list_servings", fetch)
    if incomplete:
        with pytest.raises(ResolutionIncompleteError):
            client.servings.list("Workspace", limit=50)
        assert calls == [(1, 20), (2, 20)]
    else:
        first = client.servings.list("Workspace", limit=5)
        assert first.total == 200 and calls == [(1, 20)]
        second = client.servings.list("Workspace", limit=20, cursor=first.next_cursor)
        assert [r.name for r in second.items] == [f"service-{i}" for i in range(5, 25)]
        assert calls == [(1, 20), (1, 20), (2, 20)]
        calls.clear()
        assert len(tuple(client.servings.iter("Workspace", max_items=60))) == 60
        assert calls == [(1, 20), (2, 20), (3, 20)]


def test_name_resolution_filters_pages_and_deduplicates(client, catalog, monkeypatch):
    from inspire import AmbiguousResourceError

    rows = [ServingInfo(f"s{i}", "prefix-name", "running") for i in range(20)]
    rows += [ServingInfo("chosen", "name", "running")] * 2
    calls = []

    def fetch(**kwargs):
        assert kwargs["keyword"] == "name"
        page, size = kwargs["page"], kwargs["page_size"]
        calls.append((page, size))
        return rows[(page - 1) * size:page * size], len(rows)

    monkeypatch.setattr(api, "list_servings", fetch)
    monkeypatch.setattr(client.servings, "_binding", replace(
        client.servings._binding, get_detail=lambda *a, **kw: {
            "inference_serving_id": "chosen", "name": "name", "status": "running",
        },
    ))
    assert client.servings.get("name", workspace="Workspace").ref.key == "chosen"
    assert calls == [(1, 20), (2, 20)]
    rows.append(ServingInfo("other", "name", "running"))
    with pytest.raises(AmbiguousResourceError) as error:
        client.servings.get("name", workspace="Workspace")
    assert {row.ref.key for row in error.value.candidates} == {"chosen", "other"}


def test_name_lookup_queries_only_keyword_results(client, catalog, monkeypatch):
    rows = [ServingInfo(f"s{i}", f"service-{i}", "running") for i in range(200)]
    calls = []

    def fetch(**kwargs):
        calls.append(kwargs)
        assert kwargs["page_size"] == 20
        matches = [r for r in rows if kwargs["keyword"] in r.name]
        return matches, len(matches)

    monkeypatch.setattr(api, "list_servings", fetch)
    monkeypatch.setattr(client.servings, "_binding", replace(
        client.servings._binding, get_detail=lambda *a, **kw: {
            "inference_serving_id": "s199", "name": "service-199", "status": "running",
        },
    ))
    assert client.servings.get("service-199", workspace="Workspace").ref.key == "s199"
    assert len(calls) == 1 and calls[0]["keyword"] == "service-199"


def test_serving_create_accepts_a_typed_model_reference(client, catalog, monkeypatch):
    # A ModelRef spec must resolve through Models.get, whose workspace is keyword-only.
    from inspire import ModelRef

    from inspire.platform.web.browser_api.models import ModelInfo

    monkeypatch.setattr(
        api,
        "list_models",
        lambda **kw: (
            [ModelInfo(model_id="model-test", name="Model", status="READY", latest_version="3")],
            1,
        ),
    )
    ref = ModelRef("Model", client.account, client.base_url, "model-test", "ws-test")
    captured = []
    monkeypatch.setattr(
        api, "create_serving", lambda **kw: captured.append(kw) or {"id": "serving-test"}
    )
    plan = client.servings.plan(replace(catalog.serving, model=ref))
    assert plan.model == "Model"
    assert plan.model_version == 3
    handle = client.servings.create(replace(catalog.serving, model=ref))
    assert handle.ref.key == "serving-test"
    assert captured[0]["model_id"] == "model-test"
