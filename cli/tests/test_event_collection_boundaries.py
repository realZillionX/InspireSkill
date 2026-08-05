from __future__ import annotations

import importlib
import json

from click.testing import CliRunner

from inspire.cli.commands.job import job_events
from inspire.cli.commands.ray import ray_commands
from inspire.cli.main import main as cli_main
from inspire.cli.utils import notebook_cli as notebook_cli_module
from inspire.platform.web.browser_api import jobs as jobs_api
from inspire.platform.web.browser_api import ray_jobs as ray_jobs_api

project_commands = importlib.import_module(
    "inspire.cli.commands.project.project_commands"
)


class _Session:
    all_workspace_ids = ["ws-test"]
    workspace_id = "ws-test"
    all_workspace_names = {"ws-test": "Test Workspace"}


def _json_data(output: str) -> dict:
    payload = json.loads(output)
    return payload.get("data", payload)


def test_all_job_instance_names_pages_past_200(monkeypatch) -> None:  # noqa: ANN001
    calls: list[int] = []

    def fake_list(job_id, *, limit, page_num, session):  # noqa: ANN001
        calls.append(page_num)
        start = (page_num - 1) * limit
        stop = min(start + limit, 450)
        return ([{"name": f"worker-{index}"} for index in range(start, stop)], 450)

    monkeypatch.setattr(job_events.browser_api_module, "list_job_instances", fake_list)

    names = job_events._list_all_job_instance_names("job-internal", session=object())

    assert calls == [1, 2, 3]
    assert len(names) == 450
    assert names[-1] == "worker-449"


def test_job_instance_events_pages_and_chunks_pod_names(monkeypatch) -> None:  # noqa: ANN001
    calls: list[dict] = []

    def fake_request(session, method, path, *, referer, body=None, timeout=30):  # noqa: ANN001
        calls.append(body)
        page_num = body["page_num"]
        count = 2 if page_num == 1 else 1
        return {
            "code": 0,
            "data": {
                "items": [
                    {"message": f"{body['filter']['object_ids'][0]}-{page_num}-{index}"}
                    for index in range(count)
                ],
                "total": 3,
            },
        }

    monkeypatch.setattr(jobs_api, "_request_json", fake_request)

    events = jobs_api.list_job_instance_events(
        "job-internal",
        [f"worker-{index}" for index in range(201)],
        page_size=2,
        session=object(),
    )

    assert len(events) == 6
    assert [(call["page_num"], len(call["filter"]["object_ids"])) for call in calls] == [
        (1, 200),
        (2, 200),
        (1, 1),
        (2, 1),
    ]


def test_ray_event_api_paginates_with_finite_pages(monkeypatch) -> None:  # noqa: ANN001
    calls: list[int] = []

    def fake_request(session, method, path, *, referer, body=None, timeout=30):  # noqa: ANN001
        calls.append(body["page_num"])
        count = 1 if body["page_num"] == 3 else 2
        return {
            "code": 0,
            "data": {
                "items": [
                    {"message": f"event-{body['page_num']}-{index}"}
                    for index in range(count)
                ],
                "total": 5,
            },
        }

    monkeypatch.setattr(ray_jobs_api, "_request_json", fake_request)

    events = ray_jobs_api.list_ray_job_events(
        "ray-internal",
        page_size=2,
        max_pages=3,
        session=object(),
    )

    assert calls == [1, 2, 3]
    assert len(events) == 5


def test_ray_events_uses_bounded_name_scan_and_recent_event_pages(monkeypatch) -> None:  # noqa: ANN001
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        ray_commands.Config,
        "from_files_and_env",
        lambda **_kwargs: (object(), []),
    )
    monkeypatch.setattr(ray_commands, "get_web_session", lambda: object())

    def fake_resolve(*_args, **kwargs):  # noqa: ANN001
        captured["resolution_limit"] = kwargs["limit"]
        return "ray-internal"

    monkeypatch.setattr(ray_commands, "_resolve_ray_name_in_workspace", fake_resolve)

    def fake_events(ray_job_id, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return [{"message": "newest"}, {"message": "older"}]

    monkeypatch.setattr(ray_commands.browser_api_module, "list_ray_job_events", fake_events)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "ray", "events", "ray-name", "--workspace", "Test Workspace"],
    )

    assert result.exit_code == 0, result.output
    assert captured["resolution_limit"] == 500
    assert captured["page_size"] == 200
    assert captured["max_pages"] == 5
    assert captured["sort_ascending"] is False
    assert [event["message"] for event in _json_data(result.output)["items"]] == [
        "older",
        "newest",
    ]


def test_project_owners_default_limit_all_and_conflict(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        notebook_cli_module.web_session_module,
        "get_web_session",
        lambda: _Session(),
    )
    monkeypatch.setattr(
        project_commands.browser_api_module,
        "list_project_owners",
        lambda session=None: [
            {
                "name": f"Owner {index}",
                "extra_info": {"login_name": f"owner-{index}"},
            }
            for index in range(25)
        ],
    )

    limited = CliRunner().invoke(cli_main, ["--json", "project", "owners"])
    assert limited.exit_code == 0, limited.output
    limited_data = _json_data(limited.output)
    assert len(limited_data["items"]) == 20
    assert limited_data["shown"] == 20
    assert limited_data["total"] == 25
    assert limited_data["truncated"] is True

    complete = CliRunner().invoke(cli_main, ["--json", "project", "owners", "--all"])
    assert complete.exit_code == 0, complete.output
    complete_data = _json_data(complete.output)
    assert len(complete_data["items"]) == 25
    assert "truncated" not in complete_data

    conflict = CliRunner().invoke(
        cli_main,
        ["project", "owners", "--all", "--limit", "3"],
    )
    assert conflict.exit_code != 0
    assert "Use either --limit or --all, not both." in conflict.output
