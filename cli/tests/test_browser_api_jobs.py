from __future__ import annotations

from typing import Any

import pytest

from inspire.platform.web.browser_api import jobs as jobs_module


class _FakeSession:
    workspace_id = "ws-default"
    storage_state = {"cookies": [{"name": "session", "value": "ok"}]}


def test_job_api_validation_uses_visible_selection_terms() -> None:
    with pytest.raises(ValueError, match="Job selection is required\\."):
        jobs_module.get_job_detail_v2("", session=_FakeSession())

    with pytest.raises(ValueError, match="Workspace selection is required\\."):
        jobs_module.list_jobs(created_by="current-user", session=_FakeSession())


def test_list_train_job_logs_uses_string_epoch_ms(monkeypatch) -> None:  # noqa: ANN001
    captured: dict[str, Any] = {}

    def fake_request_json(session, method, path, *, referer, body=None, timeout=30):  # noqa: ANN001
        captured.update(
            {
                "session": session,
                "method": method,
                "path": path,
                "referer": referer,
                "body": body,
                "timeout": timeout,
            }
        )
        return {
            "Result": {
                "logs": [{"pod_name": "pod-a", "message": "hello"}],
                "total": 1,
            },
        }

    monkeypatch.setattr(jobs_module, "_request_json", fake_request_json)

    logs, total = jobs_module.list_train_job_logs(
        pod_names=["pod-a"],
        start_timestamp_ms=123,
        end_timestamp_ms=456,
        page_size=7,
        job_id="job-abc",
        session=_FakeSession(),
    )

    assert total == 1
    assert logs[0]["message"] == "hello"
    assert captured["method"] == "POST"
    assert captured["path"].endswith("/api/v2/train?Action=GetJobLog")
    assert captured["body"] == {
        "page_size": 7,
        "filter": {
            "podNames": ["pod-a"],
            "start_timestamp_ms": "123",
            "end_timestamp_ms": "456",
        },
    }
    assert "distributedTrainingDetail/job-abc" in captured["referer"]


def test_list_jobs_passes_keyword(monkeypatch) -> None:  # noqa: ANN001
    captured: dict[str, Any] = {}

    def fake_request_json(session, method, path, *, referer, body=None, timeout=30):  # noqa: ANN001
        captured.update(
            {
                "method": method,
                "path": path,
                "referer": referer,
                "body": body,
            }
        )
        return {
            "ResponseMetadata": {"Action": "ListJobs"},
            "Result": {
                "jobs": [
                    {
                        "job_id": "job-abc",
                        "name": "qwen35-train",
                        "status": "RUNNING",
                        "created_at": "1770000000000",
                        "framework_config": [{"gpu_count": 1}],
                    }
                ],
                "total": 1,
            },
        }

    monkeypatch.setattr(jobs_module, "_request_json", fake_request_json)

    jobs, total = jobs_module.list_jobs(
        workspace_id="ws-x",
        created_by="user-x",
        keyword="qwen35",
        session=_FakeSession(),
    )

    assert len(jobs) == 1
    assert jobs[0].name == "qwen35-train"
    assert total == 1
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v2/train?Action=ListJobs"
    assert captured["body"]["workspace_id"] == "ws-x"
    assert captured["body"]["created_by"] == "user-x"
    assert captured["body"]["keyword"] == "qwen35"
    assert "distributedTraining" in captured["referer"]


def test_get_job_detail_v2_uses_action_api(monkeypatch) -> None:  # noqa: ANN001
    captured: dict[str, Any] = {}

    def fake_request_json(session, method, path, *, referer, body=None, timeout=30):  # noqa: ANN001
        captured.update(
            {
                "session": session,
                "method": method,
                "path": path,
                "referer": referer,
                "body": body,
                "timeout": timeout,
            }
        )
        return {
            "ResponseMetadata": {"Action": "GetJob"},
            "Result": {"job_id": "job-abc", "name": "train-a", "status": "RUNNING"},
        }

    monkeypatch.setattr(jobs_module, "_request_json", fake_request_json)

    detail = jobs_module.get_job_detail_v2("job-abc", session=_FakeSession())

    assert detail == {"job_id": "job-abc", "name": "train-a", "status": "RUNNING"}
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v2/train?Action=GetJob"
    assert captured["body"] == {"job_id": "job-abc"}
    assert "distributedTrainingDetail/job-abc" in captured["referer"]
