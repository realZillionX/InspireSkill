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


def test_delete_job_uses_action_api(monkeypatch) -> None:  # noqa: ANN001
    captured: dict[str, Any] = {}

    def fake_request_json(session, method, path, *, referer, body=None, timeout=30):  # noqa: ANN001
        captured.update({"method": method, "path": path, "referer": referer, "body": body})
        return {
            "ResponseMetadata": {"Action": "DeleteJob"},
            "Result": {"job_id": "job-abc"},
        }

    monkeypatch.setattr(jobs_module, "_request_json", fake_request_json)

    assert jobs_module.delete_job("job-abc", session=_FakeSession()) == {"job_id": "job-abc"}
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v2/train?Action=DeleteJob"
    assert captured["body"] == {"job_id": "job-abc"}
    assert "/jobs/distributedTraining" in captured["referer"]


def test_delete_job_surfaces_running_conflict(monkeypatch) -> None:  # noqa: ANN001
    # The platform refuses to delete a job that is still running; the wrapper
    # must let that reach the caller rather than swallow it.
    def fake_request_json(session, method, path, *, referer, body=None, timeout=30):  # noqa: ANN001
        return {
            "ResponseMetadata": {
                "Error": {"Code": "Conflict", "Message": "当前状态（运行中）无法删除，请先停止后再删除"}
            }
        }

    monkeypatch.setattr(jobs_module, "_request_json", fake_request_json)

    with pytest.raises(ValueError, match="Conflict"):
        jobs_module.delete_job("job-abc", session=_FakeSession())


def test_list_tensorboards_uses_the_pascal_case_page_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ListTensorboards` reads `PageNumber`; `page` and `page_num` are ignored."""
    from inspire.platform.web.browser_api import jobs as jobs_module

    sent: dict = {}

    def _fake(session, method, path, *, referer, body=None, timeout=30):  # noqa: ANN001
        sent["body"] = body
        sent["path"] = path
        return {"Result": {"items": [], "total": "0"}}

    monkeypatch.setattr(jobs_module, "_request_json", _fake)
    jobs_module.list_tensorboards(
        workspace_id="ws-1", created_by="user-1", page_num=2, page_size=7, session=object()
    )

    assert "Action=ListTensorboards" in sent["path"]
    assert sent["body"]["PageNumber"] == 2
    assert sent["body"]["page_size"] == 7
    # Without it the Action reports a workspace-wide total against an empty
    # list, which reads as "you have none".
    assert sent["body"]["created_by"] == "user-1"
    assert "page" not in sent["body"] and "page_num" not in sent["body"]


def test_list_tensorboards_projects_rows_without_the_status_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inspire.platform.web.browser_api import jobs as jobs_module

    def _fake(session, method, path, *, referer, body=None, timeout=30):  # noqa: ANN001
        return {
            "Result": {
                "items": [
                    {
                        "name": "tb-a",
                        "status": "tb_status_running",
                        "job_name": "train-a",
                        "tb_summary_path": "/inspire/hdd/project/p/u/logs",
                        "logic_compute_group_name": "H200",
                        "created_at": "1769591284000",
                    }
                ],
                "total": "6",
            }
        }

    monkeypatch.setattr(jobs_module, "_request_json", _fake)
    boards, total = jobs_module.list_tensorboards(
        workspace_id="ws-1", created_by="user-1", session=object()
    )

    assert total == 6
    assert boards[0].status == "running"
    assert boards[0].summary_path == "/inspire/hdd/project/p/u/logs"
    assert boards[0].job_name == "train-a"


def test_list_tensorboards_requires_a_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    from inspire.platform.web.browser_api import jobs as jobs_module

    with pytest.raises(ValueError, match="Workspace"):
        jobs_module.list_tensorboards(workspace_id=None, created_by="u", session=object())
