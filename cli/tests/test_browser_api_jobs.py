from __future__ import annotations

from typing import Any

from inspire.platform.web.browser_api import jobs as jobs_module


class _FakeSession:
    workspace_id = "ws-default"
    storage_state = {"cookies": [{"name": "session", "value": "ok"}]}


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
            "code": 0,
            "data": {
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
    assert captured["path"].endswith("/logs/train")
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
        captured["body"] = body
        return {"code": 0, "data": {"jobs": [], "total": 0}}

    monkeypatch.setattr(jobs_module, "_request_json", fake_request_json)

    jobs, total = jobs_module.list_jobs(
        workspace_id="ws-x",
        created_by="user-x",
        keyword="qwen35",
        session=_FakeSession(),
    )

    assert jobs == []
    assert total == 0
    assert captured["body"]["workspace_id"] == "ws-x"
    assert captured["body"]["created_by"] == "user-x"
    assert captured["body"]["keyword"] == "qwen35"
