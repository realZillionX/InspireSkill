"""Unit tests for `inspire.platform.web.browser_api.metrics`.

The endpoint has no public contract; these tests pin the wire-format we
reverse-engineered from the 资源视图 tab in the web UI:

- request body shape with flat ``task_type`` / ``task_ids`` / ``metric_types``
  / ``time_range`` keys, and no ``logic_compute_group_id``
- chunking at five metric types per request, because a sixth makes
  ``GetTaskMetricBatch`` answer ``InternalError``
- the ``task_metrics`` envelope, and that groups belonging to another task id
  are not attributed to ours
- tolerance of the upstream response-key typo ``time_seris_metric_groups``
- raise on ``code != 0`` and on unknown metric / task_type enums
"""

from __future__ import annotations


import pytest

from inspire.platform.web.browser_api import metrics as metrics_module
from inspire.platform.web.browser_api.metrics import (
    INTERVAL_CHOICES,
    METRIC_TYPES,
    TASK_TYPE_BY_RESOURCE,
    MetricGroup,
    MetricSample,
    get_resource_metrics_by_time,
)


class _FakeSession:
    """Session stand-in; wrappers only need it as an opaque handle."""

    def __init__(self) -> None:
        self.workspace_id = "ws-fake"


def _install_fake_request(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[dict],
    calls: list[dict],
) -> None:
    iterator = iter(responses)

    def _fake(session, method, url, *, referer=None, body=None, timeout=30, **kwargs):
        calls.append({
            "session": session,
            "method": method,
            "url": url,
            "referer": referer,
            "body": body,
            "timeout": timeout,
        })
        return next(iterator)

    monkeypatch.setattr(metrics_module, "_request_json", _fake)


def _group(metric_type: str, samples: list[tuple[int, float]]) -> dict:
    return {
        "group_name": "pod-xyz",
        "metric_type": metric_type,
        "resource_name": "GPU",
        "time_series": [{"timestamp": str(ts), "data": value} for ts, value in samples],
    }


def _success_response(
    metric_type: str,
    *,
    samples: list[tuple[int, float]],
    task_id: str = "nb-abc",
) -> dict:
    return _batch_response({task_id: [(metric_type, samples)]})


def _batch_response(
    by_task: dict[str, list[tuple[str, list[tuple[int, float]]]]],
    *,
    key: str = "time_seris_metric_groups",
) -> dict:
    """A `GetTaskMetricBatch` envelope: groups nested per task under a list."""
    return {
        "code": 0,
        "message": "success",
        "data": {
            "task_metrics": [
                {
                    "task_id": task_id,
                    key: [_group(metric, samples) for metric, samples in entries],
                }
                for task_id, entries in by_task.items()
            ]
        },
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_get_resource_metrics_packs_metrics_into_one_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    _install_fake_request(
        monkeypatch,
        responses=[
            _batch_response(
                {
                    "nb-abc": [
                        ("gpu_usage_rate", [(100, 0.25), (160, 0.75)]),
                        ("cpu_usage_rate", [(100, 0.05)]),
                    ]
                }
            )
        ],
        calls=calls,
    )

    session = _FakeSession()
    groups = get_resource_metrics_by_time(
        task_id="nb-abc",
        task_type=TASK_TYPE_BY_RESOURCE["notebook"],
        logic_compute_group_id="lcg-test",
        metric_types=["gpu_usage_rate", "cpu_usage_rate"],
        start_timestamp=100,
        end_timestamp=200,
        interval_second=60,
        session=session,
    )

    # Both metrics ride one request; the singular Action needed one apiece.
    assert len(calls) == 1
    first = calls[0]
    assert first["method"] == "POST"
    # v2 has no cluster-wide metric endpoint; the route comes from task_type.
    assert first["url"].endswith("/api/v2/notebook?Action=GetTaskMetricBatch")
    assert first["referer"].endswith("/jobs/interactiveModelDetail/nb-abc")
    assert first["body"]["task_type"] == "interactive_modeling"
    assert first["body"]["task_ids"] == ["nb-abc"]
    assert first["body"]["metric_types"] == ["gpu_usage_rate", "cpu_usage_rate"]
    # This Action takes no compute group; sending one is a `unknown field` risk.
    assert "logic_compute_group_id" not in first["body"]
    assert "filter" not in first["body"]
    assert first["body"]["time_range"] == {
        "start_timestamp": 100,
        "end_timestamp": 200,
        "interval_second": 60,
    }

    # Flat list of MetricGroup preserves upstream order.
    assert len(groups) == 2
    assert [g.metric_type for g in groups] == ["gpu_usage_rate", "cpu_usage_rate"]
    assert groups[0].samples == [
        MetricSample(timestamp=100, value=0.25),
        MetricSample(timestamp=160, value=0.75),
    ]


def test_get_resource_metrics_chunks_above_five_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sixth metric type makes the platform answer InternalError."""
    calls: list[dict] = []
    _install_fake_request(
        monkeypatch,
        responses=[
            _batch_response({"nb-abc": [(m, [(100, 0.5)]) for m in METRIC_TYPES[:5]]}),
            _batch_response({"nb-abc": [(m, [(100, 0.5)]) for m in METRIC_TYPES[5:]]}),
        ],
        calls=calls,
    )

    groups = get_resource_metrics_by_time(
        task_id="nb-abc",
        task_type="interactive_modeling",
        logic_compute_group_id="lcg-test",
        metric_types=list(METRIC_TYPES),
        start_timestamp=0,
        end_timestamp=100,
        interval_second=60,
        session=_FakeSession(),
    )

    # Eight metrics, two requests -- never one request carrying six.
    assert len(calls) == 2
    assert [len(c["body"]["metric_types"]) for c in calls] == [5, 3]
    assert calls[0]["body"]["metric_types"] == list(METRIC_TYPES[:5])
    assert calls[1]["body"]["metric_types"] == list(METRIC_TYPES[5:])
    assert [g.metric_type for g in groups] == list(METRIC_TYPES)


def test_get_resource_metrics_ignores_other_tasks_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch answer is keyed by task; another task's series is not ours."""
    calls: list[dict] = []
    _install_fake_request(
        monkeypatch,
        responses=[
            _batch_response(
                {
                    "nb-abc": [("gpu_usage_rate", [(100, 0.25)])],
                    "nb-other": [("gpu_usage_rate", [(100, 0.99)])],
                }
            )
        ],
        calls=calls,
    )

    groups = get_resource_metrics_by_time(
        task_id="nb-abc",
        task_type="interactive_modeling",
        logic_compute_group_id="lcg-test",
        metric_types=["gpu_usage_rate"],
        start_timestamp=0,
        end_timestamp=100,
        interval_second=60,
        session=_FakeSession(),
    )
    assert len(groups) == 1
    assert groups[0].samples == [MetricSample(timestamp=100, value=0.25)]


def test_get_resource_metrics_accepts_fixed_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accept both the current response key and its corrected spelling."""
    calls: list[dict] = []
    _install_fake_request(
        monkeypatch,
        responses=[
            _batch_response(
                {"nb-abc": [("gpu_usage_rate", [(100, 0.5)])]},
                key="time_series_metric_groups",
            )
        ],
        calls=calls,
    )

    groups = get_resource_metrics_by_time(
        task_id="nb-abc",
        task_type="interactive_modeling",
        logic_compute_group_id="lcg-test",
        metric_types=["gpu_usage_rate"],
        start_timestamp=0,
        end_timestamp=100,
        interval_second=60,
        session=_FakeSession(),
    )
    assert len(groups) == 1
    assert groups[0].samples == [MetricSample(timestamp=100, value=0.5)]


# ---------------------------------------------------------------------------
# Referer per task_type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "task_type,referer_suffix",
    [
        ("interactive_modeling", "/jobs/interactiveModelDetail/nb-abc"),
        ("distributed_training", "/jobs/distributedTrainingDetail/nb-abc"),
        ("hpc_job", "/jobs/hpcDetail/nb-abc"),
        ("inference_serving", "/jobs/modelDeploymentDetail/nb-abc"),
    ],
)
def test_referer_matches_task_type(
    monkeypatch: pytest.MonkeyPatch, task_type: str, referer_suffix: str
) -> None:
    calls: list[dict] = []
    _install_fake_request(
        monkeypatch,
        responses=[_success_response("gpu_usage_rate", samples=[(0, 0.0)])],
        calls=calls,
    )

    get_resource_metrics_by_time(
        task_id="nb-abc",
        task_type=task_type,
        logic_compute_group_id="lcg-test",
        metric_types=["gpu_usage_rate"],
        start_timestamp=0,
        end_timestamp=100,
        interval_second=60,
        session=_FakeSession(),
    )

    assert calls[0]["referer"].endswith(referer_suffix)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_get_resource_metrics_rejects_unknown_metric() -> None:
    with pytest.raises(ValueError, match="unknown metric_type"):
        get_resource_metrics_by_time(
            task_id="nb-abc",
            task_type="interactive_modeling",
            logic_compute_group_id="lcg-test",
            metric_types=["bogus_metric"],
            start_timestamp=0,
            end_timestamp=100,
            interval_second=60,
            session=_FakeSession(),
        )


def test_get_resource_metrics_rejects_unknown_task_type() -> None:
    with pytest.raises(ValueError, match="unknown task_type"):
        get_resource_metrics_by_time(
            task_id="nb-abc",
            task_type="training_job",  # rejected form — see probe notes
            logic_compute_group_id="lcg-test",
            metric_types=["gpu_usage_rate"],
            start_timestamp=0,
            end_timestamp=100,
            interval_second=60,
            session=_FakeSession(),
        )


def test_get_resource_metrics_raises_on_api_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    _install_fake_request(
        monkeypatch,
        responses=[{"code": 100000, "message": "422: prom backend down", "data": None}],
        calls=calls,
    )

    with pytest.raises(ValueError, match="prom backend"):
        get_resource_metrics_by_time(
            task_id="nb-abc",
            task_type="interactive_modeling",
            logic_compute_group_id="lcg-test",
            metric_types=["gpu_usage_rate"],
            start_timestamp=0,
            end_timestamp=100,
            interval_second=60,
            session=_FakeSession(),
        )


# ---------------------------------------------------------------------------
# Static surface
# ---------------------------------------------------------------------------


def test_exported_constants_stay_in_sync() -> None:
    # 8 metrics the UI exposes; keeping them enumerated here guards against
    # silent regressions (e.g. if a rename lands in metrics.py but the
    # platform hasn't changed yet).
    assert METRIC_TYPES == (
        "gpu_usage_rate",
        "gpu_memory_usage_rate",
        "cpu_usage_rate",
        "memory_usage_rate",
        "disk_io_read",
        "disk_io_write",
        "network_tcp_ip_io_read",
        "network_tcp_ip_io_write",
    )
    assert set(INTERVAL_CHOICES) == {"1m", "5m", "30m", "1h"}
    assert TASK_TYPE_BY_RESOURCE == {
        "notebook": "interactive_modeling",
        "job": "distributed_training",
        "hpc": "hpc_job",
        "serving": "inference_serving",
        "ray": "ray_job",
    }


def test_metric_group_from_api_skips_malformed_samples() -> None:
    group = MetricGroup.from_api(
        {
            "group_name": "pod",
            "metric_type": "gpu_usage_rate",
            "resource_name": "GPU",
            "time_series": [
                {"timestamp": "100", "data": 0.25},
                "not-a-dict",
                {"timestamp": "bad", "data": 0.5},
                {"timestamp": "200"},  # no data -> defaults to 0.0
            ],
        }
    )
    assert group.samples == [
        MetricSample(timestamp=100, value=0.25),
        MetricSample(timestamp=200, value=0.0),
    ]
