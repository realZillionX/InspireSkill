"""Wire-contract tests for the Ray observation Actions.

``ray.GetJobLog`` is the one Action on ``/api/v2/ray`` that breaks the route's
own rule. Every sibling keys on ``ray_job_id``; ``GetJobLog`` does not declare
that field at all (``unknown field "ray_job_id"``), declares ``job_id`` instead
— which scopes nothing — and takes its actual scope from ``filter.podNames``.
These tests pin that body so a later "consistency" cleanup can't quietly add an
id key back and start querying the wrong thing.

``ListJobScalingHistories`` pins the top-level ``worker_group_name`` filter: it
declares neither ``filter`` nor ``sorter``, so a nested envelope would be
rejected outright.
"""

from __future__ import annotations

from typing import Any

import pytest

from inspire.platform.web.browser_api import ray_jobs as ray_jobs_module
from inspire.platform.web.browser_api.ray_jobs import (
    list_ray_job_logs,
    list_ray_job_scaling_histories,
)


class _FakeSession:
    workspace_id = "ws-default"


def _install_fake_request(
    monkeypatch: pytest.MonkeyPatch, response: dict, record: dict
) -> None:
    def _fake(session, method, url, *, referer=None, body=None, timeout=30, **kwargs):
        record["session"] = session
        record["method"] = method
        record["url"] = url
        record["referer"] = referer
        record["body"] = body
        return response

    monkeypatch.setattr(ray_jobs_module, "_request_json", _fake)


# ---------------------------------------------------------------------------
# list_ray_job_logs
# ---------------------------------------------------------------------------


def test_list_ray_job_logs_scopes_on_pod_names_without_any_id(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "ResponseMetadata": {"Action": "GetJobLog"},
            "Result": {
                "logs": [
                    {
                        "log_id": "log-1",
                        "pod_name": "rj-abc-head-0",
                        "time": "2026-08-15 10:00:00",
                        "message": "driver started",
                    }
                ],
                # `ray` reports paging totals as strings elsewhere on this
                # route, so the wrapper must not trust the JSON type.
                "total": "37",
            },
        },
        record,
    )

    logs, total = list_ray_job_logs(
        pod_names=["rj-abc-head-0", " ", "rj-abc-worker-1"],
        start_timestamp_ms=1_770_000_000_000,
        end_timestamp_ms=1_770_000_060_000,
        page_size=50,
        session=_FakeSession(),
    )

    assert total == 37
    assert [item["log_id"] for item in logs] == ["log-1"]
    assert record["url"].endswith("/api/v2/ray?Action=GetJobLog")
    assert record["referer"].endswith("/jobs/ray")
    assert record["body"] == {
        "page_size": 50,
        "filter": {
            "podNames": ["rj-abc-head-0", "rj-abc-worker-1"],
            # String fields carrying epoch milliseconds: an int is rejected
            # with `invalid value for string field endTimestampMs`.
            "start_timestamp_ms": "1770000000000",
            "end_timestamp_ms": "1770000060000",
        },
    }
    assert "ray_job_id" not in record["body"]
    assert "job_id" not in record["body"]
    assert "sorter" not in record["body"]


def test_list_ray_job_logs_refuses_an_empty_pod_list(monkeypatch) -> None:
    """An unscoped query is a successful empty answer, so it must never go out.

    The platform replies ``{"logs": [], "total": 0}`` to a request with no pod
    names. Letting that through would report "this cluster printed nothing"
    for a request that never asked about the cluster at all.
    """
    record: dict[str, Any] = {}
    _install_fake_request(monkeypatch, {"Result": {"logs": [], "total": 0}}, record)

    with pytest.raises(ValueError, match="at least one instance name"):
        list_ray_job_logs(
            pod_names=["", "   "],
            start_timestamp_ms=1,
            end_timestamp_ms=2,
            session=_FakeSession(),
        )

    assert record == {}


def test_list_ray_job_logs_surfaces_platform_errors(monkeypatch) -> None:
    """A failure must stay distinguishable from "the platform returned empty"."""
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "ResponseMetadata": {
                "Error": {
                    "Code": "InvalidParameter",
                    "Message": (
                        "Invalid instance names, the ray job ids length of "
                        "instances expect 1, but got 0."
                    ),
                }
            }
        },
        record,
    )

    with pytest.raises(ValueError, match=r"Ray Job logs failed:.*InvalidParameter"):
        list_ray_job_logs(
            pod_names=["not-a-real-pod"],
            start_timestamp_ms=1,
            end_timestamp_ms=2,
            session=_FakeSession(),
        )


def test_list_ray_job_logs_falls_back_to_row_count_without_total(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"Result": {"logs": [{"message": "a"}, {"message": "b"}]}},
        record,
    )

    logs, total = list_ray_job_logs(
        pod_names=["rj-abc-head-0"],
        start_timestamp_ms=1,
        end_timestamp_ms=2,
        session=_FakeSession(),
    )

    assert total == 2
    assert len(logs) == 2


def test_list_ray_job_logs_reads_logs_not_items(monkeypatch) -> None:
    """The list key is per-Action; `items` is what every *other* ray Action uses."""
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"Result": {"items": [{"message": "wrong key"}], "logs": [], "total": "0"}},
        record,
    )

    logs, total = list_ray_job_logs(
        pod_names=["rj-abc-head-0"],
        start_timestamp_ms=1,
        end_timestamp_ms=2,
        session=_FakeSession(),
    )

    assert logs == []
    assert total == 0


# ---------------------------------------------------------------------------
# list_ray_job_scaling_histories — worker-group filter
# ---------------------------------------------------------------------------


def test_scaling_histories_send_worker_group_at_top_level(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "Result": {
                "items": [
                    {
                        "event_time": "1770000000000",
                        "event_type": "scale_up",
                        "replicas_before": 1,
                        "replicas_after": 4,
                    }
                ],
                "total": "1",
            }
        },
        record,
    )

    items, total = list_ray_job_scaling_histories(
        "rj-abc",
        worker_group_name="decode",
        page_num=1,
        page_size=-1,
        session=_FakeSession(),
    )

    assert total == 1
    assert items[0]["event_type"] == "scale_up"
    assert record["url"].endswith("/api/v2/ray?Action=ListJobScalingHistories")
    assert record["body"] == {
        "ray_job_id": "rj-abc",
        "page_num": 1,
        "page_size": -1,
        "worker_group_name": "decode",
    }
    assert "filter" not in record["body"]
    assert "sorter" not in record["body"]


def test_scaling_histories_default_to_every_group(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(monkeypatch, {"Result": {"items": [], "total": "0"}}, record)

    items, total = list_ray_job_scaling_histories("rj-abc", session=_FakeSession())

    assert (items, total) == ([], 0)
    assert record["body"]["worker_group_name"] == ""
