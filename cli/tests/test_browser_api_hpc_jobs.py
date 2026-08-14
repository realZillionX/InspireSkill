"""Unit tests for HPC Browser API helper endpoints."""

from __future__ import annotations

from typing import Any

import pytest

from inspire.platform.web.browser_api import hpc_jobs as hpc_jobs_module
from inspire.platform.web.browser_api.hpc_jobs import (
    list_hpc_job_instances,
    list_hpc_job_logs,
)


class _FakeSession:
    workspace_id = "ws-default"


def _install_fake_request(
    monkeypatch: pytest.MonkeyPatch, response: dict, record: dict[str, Any]
) -> None:
    def _fake(session, method, url, *, referer=None, body=None, timeout=30, **kwargs):
        record["session"] = session
        record["method"] = method
        record["url"] = url
        record["referer"] = referer
        record["body"] = body
        record["timeout"] = timeout
        return response

    monkeypatch.setattr(hpc_jobs_module, "_request_json", _fake)


def test_list_hpc_job_instances_posts_job_id_body(monkeypatch: pytest.MonkeyPatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "Result": {
                "items": [{"name": "launcher", "status": "Succeeded"}],
                "total": "1",
            },
        },
        record,
    )

    items, total = list_hpc_job_instances(
        "hpc-job-123",
        limit=25,
        session=_FakeSession(),
    )

    assert total == 1
    assert items[0]["name"] == "launcher"
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/hpc?Action=ListJobInstances")
    assert record["body"] == {
        "jobId": "hpc-job-123",
        "page_num": 1,
        "page_size": 25,
    }


def test_list_hpc_job_instances_requires_job_selection() -> None:
    with pytest.raises(ValueError, match="Job selection is required\\."):
        list_hpc_job_instances("", session=_FakeSession())


def test_list_hpc_job_instances_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError, match="limit must be positive"):
        list_hpc_job_instances("hpc-job-123", limit=0, session=_FakeSession())


def test_list_hpc_job_logs_omits_sorter(monkeypatch: pytest.MonkeyPatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "Result": {
                "logs": [{"pod_name": "launcher", "message": "hello"}],
                "total": 1,
            },
        },
        record,
    )

    logs, total = list_hpc_job_logs(
        pod_names=["launcher"],
        start_timestamp_ms=123,
        end_timestamp_ms=456,
        page_size=10,
        job_id="hpc-job-123",
        session=_FakeSession(),
    )

    assert total == 1
    assert logs[0]["message"] == "hello"
    assert record["method"] == "POST"
    assert record["url"].endswith("/api/v2/hpc?Action=GetJobLog")
    assert record["body"] == {
        "page_size": 10,
        "filter": {
            "podNames": ["launcher"],
            "start_timestamp_ms": "123",
            "end_timestamp_ms": "456",
        },
    }
    assert "sorter" not in record["body"]


def test_list_hpc_jobs_reads_a_string_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """`hpc.ListJobs` answers `total` as a string; the page length is not the total."""
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"Result": {"jobs": [{"job_id": "j-1", "job_name": "one"}], "total": "202"}},
        record,
    )

    jobs, total = hpc_jobs_module.list_hpc_jobs(
        workspace_id="ws-1",
        created_by="user-1",
        session=_FakeSession(),
    )

    assert len(jobs) == 1
    assert total == 202


def test_list_hpc_jobs_falls_back_when_total_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"Result": {"jobs": [{"job_id": "j-1", "job_name": "one"}], "total": None}},
        record,
    )

    _jobs, total = hpc_jobs_module.list_hpc_jobs(
        workspace_id="ws-1",
        created_by="user-1",
        session=_FakeSession(),
    )

    assert total == 1


def test_list_hpc_jobs_caps_page_size_at_the_gateway_maximum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Above 5000 the gateway answers `page or page_size too large`."""
    record: dict[str, Any] = {}
    _install_fake_request(monkeypatch, {"Result": {"jobs": [], "total": "0"}}, record)

    hpc_jobs_module.list_hpc_jobs(
        workspace_id="ws-1",
        created_by="user-1",
        page_size=10000,
        session=_FakeSession(),
    )

    assert record["body"]["page_size"] == 5000


def test_list_hpc_jobs_leaves_a_page_size_under_the_cap_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(monkeypatch, {"Result": {"jobs": [], "total": "0"}}, record)

    hpc_jobs_module.list_hpc_jobs(
        workspace_id="ws-1",
        created_by="user-1",
        page_size=50,
        session=_FakeSession(),
    )

    assert record["body"]["page_size"] == 50
