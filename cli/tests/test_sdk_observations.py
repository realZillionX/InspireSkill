"""Typed observations preserve service projections and CLI JSON contracts."""

from dataclasses import FrozenInstanceError
from importlib import import_module
import json
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from test_sdk import client as client
from inspire import (
    NotebookRef,
    RayJobRef,
    ServingRef,
    TensorboardRef,
    Resource,
    WorkspaceRef,
    ServingVersion,
    ServingScaleHistoryEntry,
    ServingConfigs,
    ServingInvocationInfo,
    ServingAPIMetrics,
    TensorboardTags,
    TensorboardScalars,
    RayScalingEvent,
    NotebookRun,
    TensorboardScalarPoint,
)
from inspire.cli.main import main
from inspire.config import Config
from inspire.platform.web import browser_api as api
from inspire.platform.web.browser_api import ray_jobs
from inspire.services.serving.serving_views import public_serving_version, public_scale_history_entry
from inspire.services.serving.serving_output import public_configs
from inspire.services.serving.serving_access import invocation_info
from inspire.services.serving.serving_api_metrics import group_summary
from inspire.services.ray.ray_scaling import public_ray_scaling_events, event_time
from inspire.services.notebook.notebook_output import public_runs
from inspire.services.tensorboard.tensorboard_data import collect_series, tail


def cli_json(*args):
    result = CliRunner().invoke(main, ["--json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


def frozen(value):
    name = next(key for key in vars(value) if not key.startswith("_"))
    with pytest.raises(FrozenInstanceError):
        setattr(value, name, None)


@pytest.fixture
def serving_runtime(client, monkeypatch):
    from inspire.cli.commands.serving import serving_commands as commands
    from inspire.cli.commands.serving import serving_api as access

    traffic = import_module("inspire.cli.commands.serving.serving_api_metrics")
    ref = ServingRef("demo", client.account, client.base_url, "serving-test", "ws-test")
    ws = Resource("space", WorkspaceRef("space", client.account, client.base_url, "ws-test"))
    monkeypatch.setattr(client.workspaces, "get", lambda _: ws)
    monkeypatch.setattr(
        Config, "from_files_and_env", classmethod(lambda cls, **kw: (client._config, {}))
    )
    for module in [commands, access, traffic]:
        monkeypatch.setattr(module, "get_web_session", lambda: client._transport.session)
    monkeypatch.setattr(commands, "_resolve_workspace_id", lambda *a, **kw: "ws-test")
    monkeypatch.setattr(commands, "resolve_workspace_operation_scope", lambda **kw: "ws-test")
    monkeypatch.setattr(commands, "_resolve_serving_name", lambda *a, **kw: ref.key)
    monkeypatch.setattr(
        commands,
        "_run_readonly_serving_operation",
        lambda *a, operation, **kw: operation(ref.key, client._transport.session),
    )
    return ref


@pytest.mark.parametrize("partial", [False, True])
def test_serving_versions_and_history(client, serving_runtime, monkeypatch, partial):
    ref = serving_runtime
    version = (
        {}
        if partial
        else {
            "version": "2",
            "phase": "RUNNING",
            "model_name": "model",
            "command": "python serve.py",
            "created_at": "2026-09-01",
            "replicas": 0,
            "port": "8000",
            "gpu_count": 1,
            "cpu_count": 8,
            "memory_size_gib": 32,
        }
    )
    history = (
        {}
        if partial
        else {
            "id": 7,
            "replicas_before_scale": "2",
            "replicas_after_scale": 0,
            "status": "SUCCEEDED",
            "created_at": "1786780070000",
        }
    )
    monkeypatch.setattr(api, "list_serving_versions", lambda *a, **kw: ([version], 1))
    monkeypatch.setattr(api, "list_serving_scale_history", lambda *a, **kw: ([history], 1))
    versions = client.servings.versions(ref)
    histories = client.servings.scale_history(ref)
    assert isinstance(versions[0], ServingVersion)
    assert isinstance(histories.items[0], ServingScaleHistoryEntry)
    for value, expected, command in [
        (versions[0], public_serving_version(version), "versions"),
        (histories.items[0], public_scale_history_entry(history), "scale-history"),
    ]:
        assert value.to_dict() == expected
        assert cli_json("serving", command, "demo", "--workspace", "space")["items"] == [expected]
        frozen(value)
    if partial:
        assert versions[0].version is None
        assert histories.items[0].replicas_from is None


@pytest.mark.parametrize("enabled", [None, False, True])
def test_serving_configs(client, serving_runtime, monkeypatch, enabled):
    item = {
        "name": "small",
        "gpu_count_min": 0,
        "gpu_count_max": "8",
        "cpu_count_min": 4,
        "cpu_count_max": 64,
        "memory_size_min": 16,
        "memory_size_max": 256,
        "replicas": 0,
        "auto_stop_ruleset": '{"gate":"OR","conds":[{"crit":"GPU","thresh":20,"hrs":5}]}',
    }
    payload = {"configs": {"items": [item, {}]}}
    if enabled is not None:
        payload["configs"]["enable_auto_stop"] = enabled
    monkeypatch.setattr(api, "get_serving_configs", lambda **kw: payload)
    result = client.servings.configs("space")
    expected = public_configs(payload)
    assert isinstance(result, ServingConfigs)
    assert result.to_dict() == expected
    assert result.items[0].gpu_count_min == 0
    assert result.items[1].auto_stop_rules is None
    assert result.auto_stop is enabled
    assert tuple(row.to_dict() for row in result.items) == tuple(expected["items"])
    # The CLI puts the workspace flag on each row; the SDK retains public_configs.
    cli_items = [
        {**row, **({"auto_stop": enabled} if enabled is not None else {})}
        for row in expected["items"]
    ]
    assert cli_json("serving", "configs", "--workspace", "space") == {"items": cli_items}
    frozen(result)
    frozen(result.items[0])


@pytest.mark.parametrize(
    "kind,endpoint",
    [("EXCLUSIVE", "https://serve.example"), ("CUSTOM", "https://serve.example"), ("CUSTOM", "")],
)
def test_serving_invocation(client, serving_runtime, monkeypatch, kind, endpoint):
    detail = {
        "name": "demo",
        "status": "STOPPED",
        "inference_serving_type": kind,
        "extra_info": {"service": endpoint},
    }
    monkeypatch.setattr(client.servings, "_detail", lambda _: detail)
    monkeypatch.setattr(api, "get_serving_detail", lambda *a, **kw: detail)
    result = client.servings.api(serving_runtime, affinity_key="session-1")
    expected = invocation_info(detail, "demo", affinity="session-1")
    assert isinstance(result, ServingInvocationInfo)
    assert result.to_dict() == expected
    assert result.credential_env == "INF_API_KEY"
    assert result.base_url == expected.get("base_url")
    assert result.example == expected.get("example")
    assert (
        cli_json("serving", "api", "demo", "--workspace", "space", "--affinity-key", "session-1")
        == expected
    )
    frozen(result)


def test_serving_api_metrics(client, serving_runtime, monkeypatch):
    rows = [
        {
            "metric_type": "QPS",
            "data_unit": "req/s",
            "group_name": "replica",
            "time_series": [{"data": "0"}, {"data": "4"}],
        },
        {"metric_type": "QPS", "time_series": []},
    ]
    monkeypatch.setattr(api, "get_serving_api_metrics", lambda *a, **kw: rows)
    monkeypatch.setattr("inspire.sdk.servings.time.time", lambda: 1786780070)
    result = client.servings.api_metrics(serving_runtime, metric="qps")
    expected = {
        "resource": "serving",
        "name": "demo",
        "metrics": ["QPS"],
        "time_range": {"start": 1786776470, "end": 1786780070, "interval": "1m"},
        "series": [group_summary(row) for row in rows],
    }
    assert isinstance(result, ServingAPIMetrics)
    assert result.to_dict() == expected
    assert result.metrics == ("QPS",)
    assert result.series[0].avg == 2
    assert result.series[1].min is None and result.series[1].unit is None
    assert result.time_range.to_dict() == expected["time_range"]
    assert [row.to_dict() for row in result.series] == expected["series"]
    assert (
        cli_json("serving", "api-metrics", "demo", "--workspace", "space", "--metric", "qps")
        == expected
    )
    frozen(result)
    frozen(result.series[0])


@pytest.mark.parametrize("points", [None, 0, 2])
@pytest.mark.parametrize("empty", [False, True])
def test_tensorboard_data(client, monkeypatch, points, empty):
    from inspire.cli.commands.tensorboard import tensorboard_data as commands

    board = SimpleNamespace(name="board", summary_path="/inspire/runs", url="https://board.example")
    ref = TensorboardRef("board", client.account, client.base_url, "tb-test", "ws-test")
    monkeypatch.setattr(client.tensorboards, "_live", lambda *a: board)
    monkeypatch.setattr(
        commands, "_live_board", lambda *a, **kw: (client._transport.session, board)
    )
    tags = {} if empty else {"train": ["loss"], "eval": []}
    monkeypatch.setattr(api, "read_tensorboard_runs", lambda *a, **kw: list(tags))
    monkeypatch.setattr(api, "read_tensorboard_scalar_tags", lambda *a, **kw: tags)
    monkeypatch.setattr(
        api,
        "read_tensorboard_scalar_series",
        lambda *a, **kw: [(1.0, 20, 0.5), (2.0, 10, 2.0), (3.0, 30, 0.1)],
    )
    tag_result = client.tensorboards.tags(ref)
    tag_view = {
        "name": "board",
        "summary_path": "/inspire/runs",
        "runs": list(tags),
        "scalar_tags": tags,
    }
    assert isinstance(tag_result, TensorboardTags)
    assert tag_result.to_dict() == tag_view
    assert tag_result.runs == tuple(tags)
    assert all(isinstance(values, tuple) for values in tag_result.scalar_tags.values())
    assert cli_json("tensorboard", "tags", "board", "--workspace", "space") == tag_view
    rows = collect_series(client._transport.session, board, run="", tag="")
    expected = {
        "name": "board",
        "summary_path": "/inspire/runs",
        "series": [
            {
                **{k: v for k, v in row.items() if k != "points"},
                **({"points": tail(row["points"], points)} if points else {}),
            }
            for row in rows
        ],
    }
    result = client.tensorboards.scalars(ref, points=points)
    assert isinstance(result, TensorboardScalars)
    assert result.to_dict() == expected
    args = ["--points", str(points)] if points is not None else []
    assert cli_json("tensorboard", "scalars", "board", "--workspace", "space", *args) == expected
    for series, view in zip(result.series, expected["series"]):
        assert series.to_dict() == view
        assert [point.to_list() for point in series.points] == view.get("points", [])
        if series.points:
            assert isinstance(series.points[0], TensorboardScalarPoint)
            frozen(series.points[0])
        frozen(series)
    frozen(result)
    frozen(tag_result)


def test_ray_scaling(client, monkeypatch):
    from test_ray_scaling_command import _patch_config, _patch_resolution, _FakeSession

    commands = import_module("inspire.cli.commands.ray.ray_scaling")
    _patch_config(monkeypatch)
    _patch_resolution(monkeypatch, _FakeSession())
    rows = [
        {
            "event_time": "1770000001000",
            "event_type": "scale_down",
            "worker_group_name": "decode",
            "replicas_before": 2,
            "replicas_after": 0,
        },
        {},
    ]
    monkeypatch.setattr(ray_jobs, "list_ray_job_scaling_histories", lambda *a, **kw: (rows, 2))
    monkeypatch.setattr(commands, "list_ray_job_scaling_histories", lambda *a, **kw: (rows, 2))
    ref = RayJobRef("pipeline", client.account, client.base_url, "ray-test", "ws-test")
    result = client.ray.scaling(ref)
    expected = public_ray_scaling_events(sorted(rows, key=event_time))
    assert all(isinstance(row, RayScalingEvent) for row in result)
    assert [row.to_dict() for row in result] == expected
    assert result[0].replicas_before is None and result[0].group is None
    assert result[1].replicas_after == 0
    assert cli_json("ray", "scaling", "pipeline", "--workspace", "Ray资源空间")["items"] == expected
    frozen(result[0])


def test_notebook_lifecycle(client, monkeypatch):
    commands = import_module("inspire.cli.commands.notebook.notebook_lifecycle")
    rows = [
        {"index": 2, "start_time": "2026-09-01", "end_time": "2026-09-02", "status": "STOPPED"},
        {"index": 1},
        {},
    ]
    monkeypatch.setattr(api, "list_notebook_runs", lambda *a, **kw: rows)
    monkeypatch.setattr(commands, "list_notebook_runs", lambda *a, **kw: rows)
    monkeypatch.setattr(
        import_module("inspire.cli.commands.notebook.notebook_metrics"),
        "_notebook_name_to_id",
        lambda *a, **kw: SimpleNamespace(task_id="nb-test", name="demo"),
    )
    ref = NotebookRef("demo", client.account, client.base_url, "nb-test", "ws-test")
    result = client.notebooks.lifecycle(ref)
    expected = public_runs(sorted(rows, key=lambda row: row.get("index", 0)))
    assert all(isinstance(row, NotebookRun) for row in result)
    assert [row.to_dict() for row in result] == expected
    assert result[0].index is None and result[1].status is None
    assert cli_json("notebook", "lifecycle", "demo", "--workspace", "space")["items"] == expected
    frozen(result[0])
