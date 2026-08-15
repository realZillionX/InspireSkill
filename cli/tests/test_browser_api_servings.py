"""Unit tests for `inspire.platform.web.browser_api.servings`.

The Browser API serving endpoints have no public contract, so these tests
pin the wire-format parsing we reverse-engineered from the
`/jobs/modelDeployment` page: personal list scope,
the list-or-`inference_servings` key fallback, `created_by` nested-object
flattening, and the `code != 0` error path. The live account used during
development had no servings in any of its 11 workspaces, so these unit
tests are the only coverage for the happy-path.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from inspire.cli.commands.serving.public_output import public_serving_list_item
from inspire.platform.web.browser_api import servings as servings_module
from inspire.platform.web.browser_api.servings import (
    ServingInfo,
    create_serving,
    delete_serving,
    get_serving_configs,
    get_serving_detail,
    get_serving_terms,
    list_serving_events,
    list_serving_instances,
    list_serving_logs,
    list_serving_scale_history,
    list_serving_user_project,
    list_serving_versions,
    list_servings,
    rollback_serving,
    scale_serving,
    start_serving,
    stop_serving,
)
from inspire.platform.web.browser_api.servings import get_serving_api_metrics


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeSession:
    """Session stand-in; the wrappers only read `.workspace_id`."""

    def __init__(self, workspace_id: str | None = "ws-default") -> None:
        self.workspace_id = workspace_id


def _install_fake_request(
    monkeypatch: pytest.MonkeyPatch, response: dict, record: dict
) -> None:
    """Monkey-patch the module-local `_request_json` to capture the outgoing call."""

    def _fake(session, method, url, *, referer=None, body=None, timeout=30, **kwargs):
        record["session"] = session
        record["method"] = method
        record["url"] = url
        record["referer"] = referer
        record["body"] = body
        record["timeout"] = timeout
        return response

    monkeypatch.setattr(servings_module, "_request_json", _fake)


# ---------------------------------------------------------------------------
# list_servings
# ---------------------------------------------------------------------------


def test_list_servings_posts_expected_body_and_parses_response(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "Result": {
                "inference_servings": [
                    {
                        "inference_serving_id": "sv-abc",
                        "name": "demo-serving",
                        "status": "RUNNING",
                        "replicas": 2,
                        "image": "reg/img:latest",
                        "service_type": "CUSTOM",
                        "project_id": "project-1",
                        "workspace_id": "ws-override",
                        "logic_compute_group_id": "lcg-1",
                        "created_at": "1770000000000",
                        "created_by": {
                            "id": "user-1",
                            "display_name": "Alice",
                        },
                    }
                ],
                "total": 7,
            },
        },
        record,
    )

    items, total = list_servings(
        workspace_id="ws-given",
        page=2,
        page_size=20,
        session=_FakeSession(workspace_id="ws-session-default"),
    )

    assert total == 7
    assert len(items) == 1
    item = items[0]
    assert isinstance(item, ServingInfo)
    assert item.inference_serving_id == "sv-abc"
    assert item.name == "demo-serving"
    assert item.status == "RUNNING"
    assert item.replicas == 2
    assert item.created_by_name == "Alice"
    assert "service_type" not in item.__dataclass_fields__

    # Wire-format: POST, correct endpoint, correct body.
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/inference_serving?Action=ListServings")
    assert record["body"] == {
        "page": 2,
        "page_size": 20,
        "filter_by": {"my_serving": True},
        "workspace_id": "ws-given",
    }


def test_list_servings_requires_workspace_selection() -> None:
    with pytest.raises(ValueError, match="Workspace selection is required\\."):
        list_servings(session=_FakeSession(workspace_id="ws-session"))


def test_list_servings_falls_back_to_list_key_when_inference_servings_missing(
    monkeypatch,
) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"Result": {"list": [{"id": "sv-1", "name": "x"}], "total": 1}},
        record,
    )
    items, total = list_servings(workspace_id="ws-given", session=_FakeSession())
    assert total == 1
    assert items[0].inference_serving_id == "sv-1"  # falls back from `id`


@pytest.mark.parametrize(
    "identity_fields",
    (
        {
            "created_by": {
                "id": "user-hidden",
                "username": "usr_391",
                "login_name": "253108120116",
            }
        },
        {"created_by": "student-42"},
        {"creator": "usr_391"},
        {"owner": "253108120116"},
    ),
)
def test_list_servings_does_not_promote_identity_handles_to_names(
    monkeypatch,
    identity_fields: dict[str, Any],
) -> None:
    record: dict[str, Any] = {}
    item = {"id": "sv-1", "name": "demo", **identity_fields}
    _install_fake_request(
        monkeypatch,
        {"Result": {"inference_servings": [item], "total": 1}},
        record,
    )

    items, total = list_servings(workspace_id="ws-given", session=_FakeSession())

    assert total == 1
    assert items[0].created_by_name == ""
    public = public_serving_list_item(items[0])
    assert public["created_by"] == ""
    rendered = repr(public)
    for hidden in ("user-hidden", "usr_391", "student-42", "253108120116"):
        assert hidden not in rendered


def test_list_servings_supports_current_filter_fields(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch, {"code": 0, "data": {"inference_servings": [], "total": 0}}, record
    )

    list_servings(
        workspace_id="ws-given",
        filter_by={"my_serving": False},
        keyword="qwen",
        project_ids=["project-1"],
        statuses=["RUNNING"],
        serving_types=["CUSTOM"],
        session=_FakeSession(),
    )

    assert record["body"]["filter_by"] == {
        "my_serving": True,
        "keyword": "qwen",
        "project_id": ["project-1"],
        "status": ["RUNNING"],
        "inference_serving_type": ["CUSTOM"],
    }


def test_list_servings_raises_on_nonzero_code(monkeypatch) -> None:
    _install_fake_request(monkeypatch, {"code": 1234, "message": "bad"}, {})
    with pytest.raises(ValueError, match="API error: bad"):
        list_servings(workspace_id="ws-given", session=_FakeSession())


def test_list_servings_empty_response_returns_empty_list_and_zero_total(monkeypatch) -> None:
    _install_fake_request(monkeypatch, {"Result": None}, {})
    items, total = list_servings(workspace_id="ws-given", session=_FakeSession())
    assert items == []
    assert total == 0


# ---------------------------------------------------------------------------
# get_serving_configs / list_serving_user_project / get_serving_detail
# ---------------------------------------------------------------------------


def test_get_serving_configs_uses_get_and_workspace_path(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"Result": {"enable_auto_stop": False, "items": []}},
        record,
    )
    data = get_serving_configs(workspace_id="ws-abc", session=_FakeSession())
    assert data == {"enable_auto_stop": False, "items": []}
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/inference_serving?Action=GetServingConfigByWorkspaceId")


def test_list_serving_user_project_posts_workspace_id(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"Result": {"projects": [{"id": "p1"}], "users": []}},
        record,
    )
    data = list_serving_user_project(
        workspace_id="ws-xx", session=_FakeSession()
    )
    assert data == {"projects": [{"id": "p1"}], "users": []}
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/inference_serving?Action=GetInferenceServingUserProjectList")
    assert record["body"] == {"workspace_id": "ws-xx"}


def test_get_serving_detail_uses_current_path_endpoint(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch, {"code": 0, "data": {"status": "RUNNING"}}, record
    )
    data = get_serving_detail("sv-xyz", session=_FakeSession())
    assert data == {"status": "RUNNING"}
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/inference_serving?Action=GetServing")


def test_get_serving_detail_raises_on_error(monkeypatch) -> None:
    _install_fake_request(monkeypatch, {"code": 404, "message": "not found"}, {})
    with pytest.raises(ValueError, match="API error: not found"):
        get_serving_detail("sv-missing", session=_FakeSession())


def test_serving_detail_tab_helpers_use_current_paths(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "Result": {
                "inference_servings": [{"version": 1}],
                "total": "1",
            },
        },
        record,
    )

    items, total = list_serving_versions("sv-1", session=_FakeSession())
    assert total == 1
    assert items == [{"version": 1}]
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/inference_serving?Action=ListServingVersions")

    _install_fake_request(
        monkeypatch,
        {"Result": {"items": [{"name": "pod-1"}], "total": "1"}},
        record,
    )
    items, total = list_serving_instances(
        "sv-1", page=2, page_size=25, session=_FakeSession()
    )
    assert total == 1
    assert items == [{"name": "pod-1"}]
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/inference_serving?Action=ListServingInstances")
    assert record["body"] == {
        "inference_serving_id": "sv-1",
        "page": 2,
        "page_size": 25,
    }

    _install_fake_request(
        monkeypatch,
        {"Result": {"events": [{"reason": "Scheduled"}]}},
        record,
    )
    events = list_serving_events(
        "sv-1",
        object_type="INFERENCE_SERVERLESS",
        page=3,
        page_size=50,
        session=_FakeSession(),
    )
    assert events == [{"reason": "Scheduled"}]
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/inference_serving?Action=ListServingEvents")
    assert record["body"] == {
        "page": 3,
        "page_size": 50,
        "filter": {
            "object_type": "INFERENCE_SERVERLESS",
            "object_ids": ["sv-1"],
        },
    }


def test_serving_logs_and_scale_history_omit_sorter(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"Result": {"logs": [{"message": "ready"}], "total": "1"}},
        record,
    )

    logs, total = list_serving_logs(
        pod_names=["pod-1"],
        start_timestamp_ms=123,
        end_timestamp_ms=456,
        page_size=20,
        inference_serving_id="sv-1",
        session=_FakeSession(),
    )
    assert total == 1
    assert logs == [{"message": "ready"}]
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/inference_serving?Action=GetServingLog")
    assert record["body"] == {
        "page_size": 20,
        "filter": {
            "podNames": ["pod-1"],
            "start_timestamp_ms": "123",
            "end_timestamp_ms": "456",
        },
    }
    assert "sorter" not in record["body"]

    _install_fake_request(
        monkeypatch,
        {"Result": {"list": [{"replicas": 2}], "total": 1}},
        record,
    )
    items, total = list_serving_scale_history(
        "sv-1", page=2, page_size=10, session=_FakeSession()
    )
    assert total == 1
    assert items == [{"replicas": 2}]
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/inference_serving?Action=ListServingScaleHistory")
    assert record["body"] == {
        "inference_serving_id": "sv-1",
        "page": 2,
        "page_size": 10,
    }


def test_get_serving_terms_uses_terms_path(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"Result": {"endpoint": "https://example.invalid"}},
        record,
    )

    data = get_serving_terms("sv-1", session=_FakeSession())

    assert data == {"endpoint": "https://example.invalid"}
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/inference_serving?Action=GetInferenceServingTerms")


def test_create_serving_posts_current_web_ui_payload(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"Result": {"inference_serving_id": "sv-new"}},
        record,
    )

    result = create_serving(
        workspace_id="ws-1",
        project_id="project-1",
        name="demo-svc",
        logic_compute_group_id="lcg-1",
        model_id="model-1",
        model_version=1,
        mirror_id="image-1",
        command="python -m http.server 8000",
        port=8000,
        description="demo",
        replicas=2,
        node_num_per_replica=1,
        shm_gi=16,
        task_priority=1,
        custom_domain="demo-svc",
        resource_spec_price={
            "cpu_type": "CPU_TYPE_INTEL",
            "cpu_count": 18,
            "gpu_type": "NVIDIA_H200_SXM_141G",
            "gpu_count": 1,
            "memory_size_gib": 200,
            "logic_compute_group_id": "lcg-1",
            "quota_id": "quota-1",
        },
        session=_FakeSession(),
    )

    assert result == {"inference_serving_id": "sv-new"}
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/inference_serving?Action=CreateServingConsole")
    assert record["body"] == {
        "workspace_id": "ws-1",
        "project_id": "project-1",
        "inference_serving_type": "CUSTOM",
        "name": "demo-svc",
        "logic_compute_group_id": "lcg-1",
        "model_id": "model-1",
        "model_version": 1,
        "mirror_id": "image-1",
        "command": "python -m http.server 8000",
        "port": 8000,
        "description": "demo",
        "replicas": 2,
        "node_num_per_replica": 1,
        "task_priority": 1,
        "resource_spec_price": {
            "cpu_type": "CPU_TYPE_INTEL",
            "cpu_count": 18,
            "gpu_type": "NVIDIA_H200_SXM_141G",
            "gpu_count": 1,
            "memory_size_gib": 200,
            "logic_compute_group_id": "lcg-1",
            "quota_id": "quota-1",
        },
        "custom_domain": "demo-svc",
        "shm_gi": 16,
    }


def test_create_serving_requires_resolved_task_priority() -> None:
    parameter = inspect.signature(create_serving).parameters["task_priority"]

    assert parameter.default is inspect.Parameter.empty


def test_serving_actions_use_v2_action_endpoint(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(monkeypatch, {"code": 0, "data": {"ok": True}}, record)

    assert stop_serving("sv-1", session=_FakeSession()) == {"ok": True}
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/inference_serving?Action=StopServing")
    # v2 rejects a `version` field on these Actions.
    assert record["body"] == {"inference_serving_id": "sv-1"}

    assert start_serving("sv-1", session=_FakeSession()) == {"ok": True}
    assert record["url"].endswith("/api/v2/inference_serving?Action=StartServing")


def test_delete_serving_uses_delete_serving_action(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(monkeypatch, {"Result": {"ok": True}}, record)

    assert delete_serving("sv-1", session=_FakeSession()) == {"ok": True}
    # v1 needed a REST-style DELETE; v2 has a first-class Action.
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/inference_serving?Action=DeleteServing")
    assert record["body"] == {"inference_serving_id": "sv-1"}


# ---------------------------------------------------------------------------
# scale / rollback / API metrics
# ---------------------------------------------------------------------------


def test_scale_serving_sends_singular_replica_field(monkeypatch) -> None:
    # `ScaleServing` takes `replica`, singular. The plural `replicas` that
    # `CreateServingConsole` uses is a different field on a different Action.
    record: dict[str, Any] = {}
    _install_fake_request(monkeypatch, {"Result": {}}, record)

    scale_serving("sv-1", replica=3, session=_FakeSession())

    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/inference_serving?Action=ScaleServing")
    assert record["body"] == {"inference_serving_id": "sv-1", "replica": 3}


def test_scale_serving_accepts_zero_replicas(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(monkeypatch, {"Result": {}}, record)

    scale_serving("sv-1", replica=0, session=_FakeSession())

    assert record["body"]["replica"] == 0


def test_rollback_serving_posts_id_and_version(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch, {"Result": {"inference_serving_id": "sv-1"}}, record
    )

    result = rollback_serving("sv-1", version=2, session=_FakeSession())

    assert record["url"].endswith("/api/v2/inference_serving?Action=RollbackServing")
    assert record["body"] == {"inference_serving_id": "sv-1", "version": 2}
    assert result == {"inference_serving_id": "sv-1"}


def test_serving_writes_unwrap_through_v2_result(monkeypatch) -> None:
    # A v1-style envelope check would swallow this as `API error: None`, which
    # is exactly how start / stop were silently broken before.
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "ResponseMetadata": {
                "Error": {"Code": "InvalidParameter", "Message": "replica too large"}
            }
        },
        record,
    )

    with pytest.raises(ValueError, match="InvalidParameter: replica too large"):
        scale_serving("sv-1", replica=99, session=_FakeSession())

    with pytest.raises(ValueError, match="InvalidParameter"):
        rollback_serving("sv-1", version=1, session=_FakeSession())


def test_get_serving_api_metrics_sends_every_metric_in_one_request(monkeypatch) -> None:
    # Unlike `GetTaskMetric`, this Action honours the whole list, so there is
    # no per-metric fan-out, and it needs no compute-group handle.
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "Result": {
                "metric_groups": [
                    {
                        "metric_type": "QPS",
                        "data_unit": "req/s",
                        "time_series": [{"timestamp": "1", "data": 1.5}],
                    },
                    "not-a-dict",
                ]
            }
        },
        record,
    )

    groups = get_serving_api_metrics(
        "sv-1",
        metric_types=["QPS", "LATENCY"],
        start_timestamp=100,
        end_timestamp=200,
        interval_second=60,
        session=_FakeSession(),
    )

    assert record["url"].endswith("/api/v2/inference_serving?Action=GetServingApiMetric")
    assert record["body"] == {
        "inference_serving_id": "sv-1",
        "metric_types": ["QPS", "LATENCY"],
        "time_range": {
            "start_timestamp": 100,
            "end_timestamp": 200,
            "interval_second": 60,
        },
    }
    assert [g["metric_type"] for g in groups] == ["QPS"]


def test_get_serving_api_metrics_rejects_resource_metric_names() -> None:
    # The two metric families share no name; `gpu_usage_rate` belongs to
    # `GetTaskMetric` and would be rejected on the wire.
    with pytest.raises(ValueError, match="unknown serving API metric"):
        get_serving_api_metrics(
            "sv-1",
            metric_types=["gpu_usage_rate"],
            start_timestamp=1,
            end_timestamp=2,
            session=_FakeSession(),
        )


def test_get_serving_api_metrics_requires_at_least_one_metric() -> None:
    with pytest.raises(ValueError, match="no metric_types provided"):
        get_serving_api_metrics(
            "sv-1",
            metric_types=[],
            start_timestamp=1,
            end_timestamp=2,
            session=_FakeSession(),
        )


def test_get_serving_api_metrics_returns_empty_without_metric_groups(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(monkeypatch, {"Result": {}}, record)

    assert (
        get_serving_api_metrics(
            "sv-1",
            metric_types=["QPS"],
            start_timestamp=1,
            end_timestamp=2,
            session=_FakeSession(),
        )
        == []
    )


# ---------------------------------------------------------------------------
# create-time options
# ---------------------------------------------------------------------------


def _create_serving_body(monkeypatch, **extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {}
    _install_fake_request(monkeypatch, {"Result": {}}, record)
    create_serving(
        workspace_id="ws-1",
        project_id="project-1",
        name="demo-svc",
        logic_compute_group_id="lcg-1",
        model_id="model-1",
        model_version=1,
        mirror_id="image-1",
        command="python serve.py",
        port=8000,
        task_priority=1,
        resource_spec_price={"cpu_count": 4},
        session=_FakeSession(),
        **extra,
    )
    body = record["body"]
    assert isinstance(body, dict)
    return body


def test_serving_create_read_only_and_autoscaling_stay_off_the_wire_when_unset(
    monkeypatch,
) -> None:
    # Sending `false` would change every create that never asked for them; the
    # platform keeps owning the default.
    body = _create_serving_body(monkeypatch)

    assert "is_publicpath_readonly" not in body
    assert "enable_auto_scaling" not in body


def test_serving_create_sends_explicit_read_only_and_autoscaling(monkeypatch) -> None:
    body = _create_serving_body(
        monkeypatch,
        is_publicpath_readonly=True,
        enable_auto_scaling=False,
    )

    # `False` is a value the caller chose; only `None` means "do not send".
    assert body["is_publicpath_readonly"] is True
    assert body["enable_auto_scaling"] is False


def test_serving_instances_read_the_nested_group_rows(monkeypatch) -> None:  # noqa: ANN001
    """Rows live under `groups[].items[]`; the flat read was silently empty."""
    record: dict = {}
    _install_fake_request(
        monkeypatch,
        {
            "Result": {
                "groups": [
                    {"items": [{"name": "frontiers/sv-1-0", "node": "cpu-nat-568"}]},
                    {"items": [{"name": "frontiers/sv-1-1", "node": "cpu-nat-569"}]},
                ],
                "total": "2",
            }
        },
        record,
    )

    items, total = list_serving_instances("sv-1", session=_FakeSession())

    assert total == 2
    assert [item["name"] for item in items] == ["frontiers/sv-1-0", "frontiers/sv-1-1"]


def test_serving_events_switch_to_the_instance_object_type(monkeypatch) -> None:  # noqa: ANN001
    """Pod ids need the namespaced name; a bare pod name answers InternalError."""
    record: dict = {}
    _install_fake_request(
        monkeypatch,
        {"Result": {"events": [{"reason": "Unhealthy"}]}},
        record,
    )

    events = list_serving_events(
        "sv-1",
        pod_names=["frontiers/sv-1-0", " ", "frontiers/sv-1-0"],
        session=_FakeSession(),
    )

    assert [event["reason"] for event in events] == ["Unhealthy"]
    assert record["body"]["filter"] == {
        "object_type": "INFERENCE_SERVING_INSTANCE",
        "object_ids": ["frontiers/sv-1-0"],
    }


def test_serving_events_refuse_an_empty_instance_selection() -> None:
    import pytest as _pytest

    with _pytest.raises(ValueError, match="Instance selection is required"):
        list_serving_events("sv-1", pod_names=[" "], session=_FakeSession())
