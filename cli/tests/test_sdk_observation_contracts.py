"""Literal observation contracts independent of the production projections."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from test_sdk import client as client
from test_sdk_observations import serving_runtime as serving_runtime

from inspire import NotebookRef, RayJobRef, TensorboardRef
from inspire.platform.web import browser_api as api


@pytest.fixture
def utc_display(monkeypatch):
    class UTCDateTime(datetime):
        @classmethod
        def fromtimestamp(cls, value, tz=None):
            return super().fromtimestamp(value, timezone.utc)

    monkeypatch.setattr("inspire.services.utils.text.datetime", UTCDateTime)


def test_serving_version_literal_projection(client, serving_runtime, monkeypatch):
    row = {
        "version": "2", "phase": "RUNNING", "model_name": "model",
        "command": "python serve.py", "created_at": "2026-09-01", "replicas": 0,
        "port": "8000", "resource_spec_price": {
            "gpu_count": 1, "cpu_count": 8, "memory_size_gib": 32,
            "gpu_info": {"gpu_type_display": "A100"},
        },
    }
    monkeypatch.setattr(api, "list_serving_versions", lambda *a, **kw: ([row], 1))
    assert client.servings.versions(serving_runtime)[0].to_dict() == {
        "version": 2, "status": "RUNNING", "model": "model", "command": "python serve.py",
        "created_at": "2026-09-01", "replicas": 0, "port": 8000,
        "resource": "8 CPU, 32 GiB, 1 GPU (A100)",
    }


def test_scale_history_literal_projection(client, serving_runtime, monkeypatch, utc_display):
    row = {
        "id": 7, "replicas_before_scale": "2", "replicas_after_scale": 0,
        "status": "SUCCEEDED", "created_at": "1788220800000",
    }
    monkeypatch.setattr(api, "list_serving_scale_history", lambda *a, **kw: ([row], 1))
    assert client.servings.scale_history(serving_runtime).items[0].to_dict() == {
        "replicas_from": 2, "replicas_to": 0, "status": "SUCCEEDED",
        "created_at": "2026-09-01 00:00:00",
    }


def test_configs_literal_projection(client, serving_runtime, monkeypatch):
    row = {
        "name": "small", "gpu_count_min": 0, "gpu_count_max": "8",
        "cpu_count_min": 4, "cpu_count_max": 64, "memory_size_min": 16,
        "memory_size_max": 256, "replicas": 0,
        "auto_stop_ruleset": '{"gate":"OR","conds":[{"crit":"GPU","thresh":20,"hrs":5}]}',
    }
    monkeypatch.setattr(api, "get_serving_configs", lambda **kw: {
        "configs": {"items": [row], "enable_auto_stop": False},
    })
    assert client.servings.configs("space").to_dict() == {
        "items": [{
            "name": "small", "gpu_count_min": 0, "gpu_count_max": "8",
            "cpu_count_min": 4, "cpu_count_max": 64, "memory_gib_min": 16,
            "memory_gib_max": 256, "replicas": 0,
            "auto_stop_rules": '{"gate":"OR","conds":[{"crit":"GPU","thresh":20,"hrs":5}]}',
        }],
        "auto_stop": False,
    }


def test_invocation_literal_projection(client, serving_runtime, monkeypatch):
    monkeypatch.setattr(client.servings, "_detail", lambda _: {
        "name": "demo", "status": "STOPPED", "inference_serving_type": "EXCLUSIVE",
        "extra_info": {"service": "https://serve.example"},
    })
    assert client.servings.api(serving_runtime, affinity_key="session-1").to_dict() == {
        "name": "demo", "status": "STOPPED", "type": "EXCLUSIVE",
        "endpoint": "https://serve.example", "credential_env": "INF_API_KEY",
        "auth_header": "Authorization", "auth_scheme": "Bearer",
        "affinity_header": "x-inspire-inference-key",
        "note": "An endpoint may remain assigned while the service is stopped; check readiness separately.",
        "base_url": "https://serve.example/v1",
        "example": "curl https://serve.example/v1/chat/completions " + "\\\n"
        + '  -H "Authorization: Bearer $INF_API_KEY" ' + "\\\n"
        + "  -H 'x-inspire-inference-key: session-1' " + "\\\n"
        + "  -H 'Content-Type: application/json' " + "\\\n"
        + "  -d " + "'" + '{"model":"model","messages":[{"role":"user","content":"Hello"}]}' + "'",
    }


def test_api_metrics_literal_projection(client, serving_runtime, monkeypatch):
    monkeypatch.setattr(api, "get_serving_api_metrics", lambda *a, **kw: [{
        "metric_type": "QPS", "data_unit": "req/s", "group_name": "replica",
        "time_series": [{"data": "0"}, {"data": "4"}, {"data": "2"}],
    }])
    monkeypatch.setattr("inspire.sdk.servings.time.time", lambda: 1786780070)
    assert client.servings.api_metrics(serving_runtime, metric="qps").to_dict() == {
        "resource": "serving", "name": "demo", "metrics": ["QPS"],
        "time_range": {"start": 1786776470, "end": 1786780070, "interval": "1m"},
        "series": [{"metric": "QPS", "count": 3, "unit": "req/s", "group": "replica",
                    "min": 0.0, "max": 4.0, "avg": 2.0, "last": 2.0, "total": 6.0}],
    }


@pytest.fixture
def tensorboard_runtime(client, monkeypatch):
    board = SimpleNamespace(name="board", summary_path="/inspire/runs", url="https://board.example")
    monkeypatch.setattr(client.tensorboards, "_live", lambda *a: board)
    monkeypatch.setattr(api, "read_tensorboard_runs", lambda *a, **kw: ["train", "eval"])
    monkeypatch.setattr(api, "read_tensorboard_scalar_tags", lambda *a, **kw: {"train": ["loss"], "eval": []})
    monkeypatch.setattr(api, "read_tensorboard_scalar_series", lambda *a, **kw: [
        (1.0, 20, 0.5), (2.0, 10, 2.0), (3.0, 30, 0.1),
    ])
    return TensorboardRef("board", client.account, client.base_url, "tb-test", "ws-test")


def test_tensorboard_tags_literal_projection(client, tensorboard_runtime):
    assert client.tensorboards.tags(tensorboard_runtime).to_dict() == {
        "name": "board", "summary_path": "/inspire/runs", "runs": ["train", "eval"],
        "scalar_tags": {"train": ["loss"], "eval": []},
    }


def test_tensorboard_scalars_literal_projection(client, tensorboard_runtime):
    assert client.tensorboards.scalars(tensorboard_runtime, points=2).to_dict() == {
        "name": "board", "summary_path": "/inspire/runs",
        "series": [{"run": "train", "tag": "loss", "count": 3,
                    "first_step": 10, "first_value": 2.0, "last_step": 30, "last_value": 0.1,
                    "min": 0.1, "max": 2.0, "points": [[20, 0.5], [30, 0.1]]}],
    }


def test_ray_scaling_literal_projection(client, monkeypatch, utc_display):
    monkeypatch.setattr("inspire.platform.web.browser_api.ray_jobs.list_ray_job_scaling_histories",
                        lambda *a, **kw: ([{
                            "event_time": "1788220800000", "event_type": "scale_down",
                            "worker_group_name": "decode", "replicas_before": 2, "replicas_after": 0,
                        }], 1))
    ref = RayJobRef("pipeline", client.account, client.base_url, "ray-test", "ws-test")
    assert [row.to_dict() for row in client.ray.scaling(ref)] == [{
        "time": "2026-09-01 00:00:00", "event": "scale_down", "group": "decode",
        "replicas_before": 2, "replicas_after": 0,
    }]


def test_notebook_lifecycle_literal_projection(client, monkeypatch):
    monkeypatch.setattr(api, "list_notebook_runs", lambda *a, **kw: [{
        "index": 2, "start_time": "2026-09-01", "end_time": "2026-09-02", "status": "STOPPED",
    }])
    ref = NotebookRef("demo", client.account, client.base_url, "nb-test", "ws-test")
    assert [row.to_dict() for row in client.notebooks.lifecycle(ref)] == [{
        "index": 2, "start_time": "2026-09-01", "end_time": "2026-09-02", "status": "STOPPED",
    }]
