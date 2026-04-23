"""Unit tests for `inspire.platform.web.browser_api.ray_jobs`.

The ``/api/v1/ray_job/*`` endpoints were reverse-engineered from the
``/jobs/ray`` (弹性计算) page. These tests pin the request shapes we found
(field naming — ``ray_job_id``, not ``id`` / ``job_id``; ``filter_by`` in
list; error handling when ``code != 0``) so future refactors can't
silently change the wire format and break a live workspace.
"""

from __future__ import annotations

from typing import Any

import pytest

from inspire.platform.web.browser_api import ray_jobs as ray_jobs_module
from inspire.platform.web.browser_api.ray_jobs import (
    RayJobInfo,
    create_ray_job,
    delete_ray_job,
    get_ray_job_detail,
    list_ray_job_events,
    list_ray_job_instances,
    list_ray_job_scaling_histories,
    list_ray_job_users,
    list_ray_jobs,
    stop_ray_job,
)


class _FakeSession:
    def __init__(self, workspace_id: str | None = "ws-default") -> None:
        self.workspace_id = workspace_id


def _install_fake_request(
    monkeypatch: pytest.MonkeyPatch, response: dict, record: dict
) -> None:
    def _fake(session, method, url, *, referer=None, body=None, timeout=30, **kwargs):
        record["session"] = session
        record["method"] = method
        record["url"] = url
        record["referer"] = referer
        record["body"] = body
        record["timeout"] = timeout
        return response

    monkeypatch.setattr(ray_jobs_module, "_request_json", _fake)


# ---------------------------------------------------------------------------
# list_ray_jobs
# ---------------------------------------------------------------------------


def test_list_ray_jobs_posts_expected_body_and_parses(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "code": 0,
            "data": {
                "items": [
                    {
                        "ray_job_id": "ray-abc-1",
                        "name": "av-pipeline",
                        "status": "RUNNING",
                        "workspace_id": "ws-override",
                        "project_id": "project-1",
                        "project_name": "demo",
                        "created_at": "1776000000000",
                        "finished_at": None,
                        "created_by": {"id": "user-1", "name": "Alice"},
                        "priority": 5,
                    }
                ],
                "total": "1",
            },
        },
        record,
    )

    jobs, total = list_ray_jobs(
        workspace_id="ws-override",
        user_ids=["user-1"],
        session=_FakeSession(),
    )

    assert total == 1
    assert len(jobs) == 1
    job = jobs[0]
    assert isinstance(job, RayJobInfo)
    assert job.ray_job_id == "ray-abc-1"
    assert job.name == "av-pipeline"
    assert job.status == "RUNNING"
    assert job.project_name == "demo"
    assert job.created_by_name == "Alice"
    assert job.priority == 5

    # Wire format assertions: list endpoint, user filter nested under `filter_by`,
    # referer matches the web UI page.
    assert record["method"] == "POST"
    assert record["url"].endswith("/ray_job/list")
    assert "/jobs/ray" in record["referer"]
    assert record["body"]["workspace_id"] == "ws-override"
    assert record["body"]["filter_by"] == {"user_id": ["user-1"]}
    assert record["body"]["page_num"] == 1
    assert record["body"]["page_size"] == 20


def test_list_ray_jobs_without_user_filter_omits_filter_by(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"code": 0, "data": {"items": [], "total": 0}},
        record,
    )

    jobs, total = list_ray_jobs(workspace_id="ws-x", session=_FakeSession())

    assert jobs == []
    assert total == 0
    assert "filter_by" not in record["body"]


def test_list_ray_jobs_falls_back_to_session_workspace(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"code": 0, "data": {"items": [], "total": 0}},
        record,
    )

    list_ray_jobs(session=_FakeSession(workspace_id="ws-from-session"))

    assert record["body"]["workspace_id"] == "ws-from-session"


def test_list_ray_jobs_raises_on_non_zero_code(monkeypatch) -> None:
    _install_fake_request(
        monkeypatch,
        {"code": 170000, "message": "workspace not accessible"},
        {},
    )

    with pytest.raises(ValueError, match="list failed"):
        list_ray_jobs(workspace_id="ws-x", session=_FakeSession())


# ---------------------------------------------------------------------------
# get_ray_job_detail / stop / delete — id field naming
# ---------------------------------------------------------------------------


def test_get_ray_job_detail_requires_ray_job_id_field(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"code": 0, "data": {"ray_job_id": "ray-1", "name": "demo", "status": "RUNNING"}},
        record,
    )

    detail = get_ray_job_detail("ray-1", session=_FakeSession())

    assert detail["ray_job_id"] == "ray-1"
    assert record["url"].endswith("/ray_job/detail")
    # The proto schema insists on `ray_job_id` — not `id` or `job_id`.
    assert record["body"] == {"ray_job_id": "ray-1"}


def test_get_ray_job_detail_rejects_empty_id() -> None:
    with pytest.raises(ValueError, match="ray_job_id is required"):
        get_ray_job_detail("  ", session=_FakeSession())


def test_stop_ray_job_posts_expected_body(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(monkeypatch, {"code": 0, "data": {}}, record)

    stop_ray_job("ray-42", session=_FakeSession())

    assert record["url"].endswith("/ray_job/stop")
    assert record["body"] == {"ray_job_id": "ray-42"}


def test_delete_ray_job_posts_expected_body(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(monkeypatch, {"code": 0, "data": {}}, record)

    delete_ray_job("ray-42", session=_FakeSession())

    assert record["url"].endswith("/ray_job/delete")
    assert record["body"] == {"ray_job_id": "ray-42"}


def test_stop_ray_job_rejects_empty_id() -> None:
    with pytest.raises(ValueError, match="ray_job_id is required"):
        stop_ray_job("", session=_FakeSession())


def test_delete_ray_job_rejects_empty_id() -> None:
    with pytest.raises(ValueError, match="ray_job_id is required"):
        delete_ray_job(None, session=_FakeSession())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# list_ray_job_users
# ---------------------------------------------------------------------------


def test_list_ray_job_users_returns_items_list(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "code": 0,
            "data": {
                "items": [
                    {"id": "user-1", "name": "Alice"},
                    {"id": "user-2", "name": "Bob"},
                ]
            },
        },
        record,
    )

    users = list_ray_job_users(workspace_id="ws-x", session=_FakeSession())

    assert len(users) == 2
    assert {u["name"] for u in users} == {"Alice", "Bob"}
    assert record["url"].endswith("/ray_job/users")
    assert record["body"] == {"workspace_id": "ws-x"}


def test_list_ray_job_users_empty_on_none(monkeypatch) -> None:
    _install_fake_request(
        monkeypatch, {"code": 0, "data": {"items": []}}, {}
    )
    assert list_ray_job_users(workspace_id="ws-x", session=_FakeSession()) == []


# ---------------------------------------------------------------------------
# RayJobInfo parsing edge cases
# ---------------------------------------------------------------------------


def test_ray_job_info_handles_missing_fields_gracefully() -> None:
    info = RayJobInfo.from_api_response({})
    assert info.ray_job_id == ""
    assert info.name == ""
    assert info.status == ""
    assert info.finished_at is None
    assert info.priority is None
    assert info.created_by_name == ""


def test_ray_job_info_accepts_alternate_id_field() -> None:
    # Some list payloads return the id under `id` rather than `ray_job_id`
    # when the frontend serialisers are stale; the wrapper should still
    # surface a usable id so downstream `detail`/`stop` calls succeed.
    info = RayJobInfo.from_api_response({"id": "ray-legacy"})
    assert info.ray_job_id == "ray-legacy"


def test_ray_job_info_coerces_priority_string_to_int() -> None:
    info = RayJobInfo.from_api_response({"priority": "7"})
    assert info.priority == 7


def test_ray_job_info_priority_none_on_garbage() -> None:
    info = RayJobInfo.from_api_response({"priority": "not-a-number"})
    assert info.priority is None


# ---------------------------------------------------------------------------
# create_ray_job — pins the reverse-engineered wire contract
# ---------------------------------------------------------------------------


def test_create_ray_job_posts_body_verbatim_and_returns_data(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "code": 0,
            "data": {
                "ray_job_id": "ray-new-1",
                "sub_code": "OK",
                "sub_msg": "created",
            },
        },
        record,
    )

    body = {
        "name": "av-pipeline",
        "description": "streaming decode + infer",
        "workspace_id": "ws-1",
        "project_id": "project-1",
        "task_priority": 9,
        "entrypoint": "python driver.py",
        "head_node": {
            "mirror_id": "img-head-1",
            "image_type": "SOURCE_PUBLIC",
            "logic_compute_group_id": "lcg-head-1",
            "quota_id": "quota-head-1",
            "shm_gi": 64,
        },
        "worker_groups": [
            {
                "group_name": "decode",
                "mirror_id": "img-decode-1",
                "image_type": "SOURCE_PUBLIC",
                "logic_compute_group_id": "lcg-decode-1",
                "min_replicas": 1,
                "max_replicas": 4,
                "quota_id": "quota-decode-1",
                "shm_gi": 32,
            }
        ],
    }

    data = create_ray_job(body, session=_FakeSession())
    assert data == {
        "ray_job_id": "ray-new-1",
        "sub_code": "OK",
        "sub_msg": "created",
    }

    # Wire format assertions — create is POST, body is sent unmodified,
    # and the referer matches the /jobs/ray origin the SPA uses.
    assert record["method"] == "POST"
    assert record["url"].endswith("/ray_job/create")
    assert "/jobs/ray" in record["referer"]
    assert record["body"] == body


def test_create_ray_job_rejects_non_dict_body() -> None:
    with pytest.raises(ValueError, match="body must be a dict"):
        create_ray_job("not-a-dict", session=_FakeSession())  # type: ignore[arg-type]


def test_create_ray_job_raises_on_non_zero_code(monkeypatch) -> None:
    _install_fake_request(
        monkeypatch,
        {"code": 100002, "message": 'proto: unknown field "image"'},
        {},
    )
    # Any dict body gets us past the type check; the backend rejects it.
    with pytest.raises(ValueError, match="create failed"):
        create_ray_job({"anything": "goes"}, session=_FakeSession())


# ---------------------------------------------------------------------------
# list_ray_job_scaling_histories — newly surfaced endpoint
# ---------------------------------------------------------------------------


def test_list_ray_job_scaling_histories_posts_expected_body(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "code": 0,
            "data": {
                "items": [
                    {"ts": "1776000000", "group_name": "decode", "from": 1, "to": 4},
                    {"ts": "1776000060", "group_name": "decode", "from": 4, "to": 2},
                ],
                "total": 2,
            },
        },
        record,
    )

    items, total = list_ray_job_scaling_histories(
        "ray-42",
        page_num=2,
        page_size=25,
        session=_FakeSession(),
    )

    assert total == 2
    assert len(items) == 2
    assert items[0]["group_name"] == "decode"
    assert record["url"].endswith("/ray_job/scaling_histories/list")
    assert record["body"] == {
        "ray_job_id": "ray-42",
        "page_num": 2,
        "page_size": 25,
    }


def test_list_ray_job_scaling_histories_rejects_empty_id() -> None:
    with pytest.raises(ValueError, match="ray_job_id is required"):
        list_ray_job_scaling_histories("", session=_FakeSession())


# ---------------------------------------------------------------------------
# list_ray_job_events — pins the bespoke body shape (no filter/object_type)
# ---------------------------------------------------------------------------


def test_list_ray_job_events_posts_top_level_ray_job_id(monkeypatch) -> None:
    """Critical wire contract: Ray events body is flat, unlike HPC / train which
    require `filter: {object_ids, object_type}`. Passing `object_type` yields
    `参数错误`. This test pins that we do NOT wrap in filter."""
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "code": 0,
            "data": {
                "items": [
                    {
                        "reason": "CreatedRayCluster",
                        "type": "Normal",
                        "message": "Created RayCluster ns/rj-abc",
                        "last_timestamp": "1776975771000",
                        "count": 1,
                    },
                    {
                        "reason": "FailedScheduling",
                        "type": "Warning",
                        "message": "0/203 nodes are unavailable: 75 Insufficient cpu.",
                        "last_timestamp": "1776975790000",
                        "count": 3,
                    },
                ],
                "total": 2,
            },
        },
        record,
    )

    events = list_ray_job_events("rj-abc", session=_FakeSession())

    assert len(events) == 2
    assert events[1]["reason"] == "FailedScheduling"
    assert record["url"].endswith("/ray_job/events/list")
    # Must be flat — NO `filter` / `object_type` wrapper (HPC-style body is
    # rejected with 参数错误 on this endpoint).
    assert "filter" not in record["body"]
    assert record["body"] == {
        "ray_job_id": "rj-abc",
        "page_num": 1,
        "page_size": -1,
        "sorter": [{"field": "last_timestamp", "sort": "ascend"}],
    }


def test_list_ray_job_events_sort_descending(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(monkeypatch, {"code": 0, "data": {"items": [], "total": 0}}, record)
    list_ray_job_events("rj-abc", sort_ascending=False, session=_FakeSession())
    assert record["body"]["sorter"] == [{"field": "last_timestamp", "sort": "descend"}]


def test_list_ray_job_events_rejects_empty_id() -> None:
    with pytest.raises(ValueError, match="ray_job_id is required"):
        list_ray_job_events("", session=_FakeSession())


# ---------------------------------------------------------------------------
# list_ray_job_instances — pod-level view
# ---------------------------------------------------------------------------


def test_list_ray_job_instances_posts_expected_body(monkeypatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "code": 0,
            "data": {
                "items": [
                    {
                        "instance_id": "rj-abc-vhd4h-head-qlrtm",
                        "instance_type": "head",
                        "status": "pending",
                        "cpu_count": 2,
                    },
                    {
                        "instance_id": "rj-abc-vhd4h-w-worker-ttrv4",
                        "instance_type": "worker",
                        "worker_group_name": "w",
                        "status": "pending",
                        "cpu_count": 2,
                    },
                ]
            },
        },
        record,
    )

    instances = list_ray_job_instances("rj-abc", session=_FakeSession())

    assert len(instances) == 2
    assert instances[0]["instance_type"] == "head"
    assert instances[1]["worker_group_name"] == "w"
    assert record["url"].endswith("/ray_job/instances/list")
    assert record["body"] == {
        "ray_job_id": "rj-abc",
        "page_num": 1,
        "page_size": -1,
    }


def test_list_ray_job_instances_rejects_empty_id() -> None:
    with pytest.raises(ValueError, match="ray_job_id is required"):
        list_ray_job_instances("", session=_FakeSession())
