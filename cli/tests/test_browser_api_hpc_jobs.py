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
            "code": 0,
            "data": {
                "items": [{"name": "launcher", "status": "Succeeded"}],
                "total": "1",
            },
        },
        record,
    )

    items, total = list_hpc_job_instances(
        "hpc-job-123",
        num=25,
        session=_FakeSession(),
    )

    assert total == 1
    assert items[0]["name"] == "launcher"
    assert record["method"] == "POST"
    assert record["url"].endswith("/hpc_jobs/instances/list")
    assert record["body"] == {
        "jobId": "hpc-job-123",
        "page_num": 1,
        "page_size": 25,
    }


def test_list_hpc_job_instances_rejects_empty_id() -> None:
    with pytest.raises(ValueError, match="job_id is required"):
        list_hpc_job_instances("", session=_FakeSession())


def test_list_hpc_job_instances_rejects_non_positive_num() -> None:
    with pytest.raises(ValueError, match="num must be positive"):
        list_hpc_job_instances("hpc-job-123", num=0, session=_FakeSession())


def test_list_hpc_job_logs_omits_sorter(monkeypatch: pytest.MonkeyPatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "code": 0,
            "data": {
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
    assert record["url"].endswith("/logs/hpc")
    assert record["body"] == {
        "page_size": 10,
        "filter": {
            "podNames": ["launcher"],
            "start_timestamp_ms": "123",
            "end_timestamp_ms": "456",
        },
    }
    assert "sorter" not in record["body"]
