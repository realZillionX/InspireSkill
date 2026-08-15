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

    rows = job_events._list_all_job_instances("job-internal", session=object())

    assert calls == [1, 2, 3]
    assert len(rows) == 450
    assert rows[-1]["name"] == "worker-449"


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


_POD_HANDLES = [
    f"job-3cdb13c4-3ec7-466b-81bd-45296e63efd0-worker-{rank}-0" for rank in range(2)
]


def _patch_job_events(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        job_events.Config,
        "from_files_and_env",
        lambda **_kwargs: (object(), {}),
    )
    monkeypatch.setattr(job_events, "_close_web_client", lambda: None)
    monkeypatch.setattr(
        job_events,
        "_run_readonly_web_job_operation",
        lambda **kwargs: kwargs["operation"]("job-internal", object()),
    )
    monkeypatch.setattr(
        job_events.browser_api_module,
        "list_job_instances",
        lambda _job_id, **_kwargs: (
            [
                {
                    "name": handle,
                    "instance_status": "instance_running",
                    "instance_type": "instance_type_worker",
                    "rank": rank,
                }
                for rank, handle in enumerate(_POD_HANDLES)
            ],
            len(_POD_HANDLES),
        ),
    )
    monkeypatch.setattr(
        job_events,
        "list_job_instance_events",
        lambda _job_id, pods, session=None: [  # noqa: ANN001
            {
                "reason": "FailedScheduling",
                "message": "0/8 nodes are available",
                "object_id": pod,
                "object_type": "instance",
                "last_timestamp": "1",
            }
            for pod in pods
        ],
    )


def test_job_events_merge_controller_and_pod_views(monkeypatch) -> None:  # noqa: ANN001
    """Two disjoint sets from one Action; the default has to read both."""
    _patch_job_events(monkeypatch)
    monkeypatch.setattr(
        job_events,
        "list_job_events",
        lambda _job_id, session=None: [  # noqa: ANN001
            {
                "reason": "SuccessfulCreatePod",
                "message": "Created pod",
                "object_type": "job",
                "last_timestamp": "0",
            }
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "job", "events", "train-a", "--workspace", "Test Workspace"],
    )

    assert result.exit_code == 0, result.output
    items = _json_data(result.output)["items"]
    assert [item["reason"] for item in items] == [
        "SuccessfulCreatePod",
        "FailedScheduling",
        "FailedScheduling",
    ]
    assert [item.get("instance") for item in items] == [None, "rank=0", "rank=1"]
    assert "worker-0-0" not in result.output


def test_job_events_narrow_to_one_rank(monkeypatch) -> None:  # noqa: ANN001
    """`--instance` speaks the Name column of `job instances`, not the pod handle."""
    _patch_job_events(monkeypatch)
    sent: list[list[str]] = []
    monkeypatch.setattr(
        job_events,
        "list_job_instance_events",
        lambda _job_id, pods, session=None: sent.append(list(pods))  # noqa: ANN001
        or [{"reason": "Scheduled", "object_id": pods[0], "last_timestamp": "1"}],
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "job",
            "events",
            "train-a",
            "--workspace",
            "Test Workspace",
            "--instance",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert sent == [[_POD_HANDLES[1]]]
    assert [item["instance"] for item in _json_data(result.output)["items"]] == ["rank=1"]


def test_job_events_reject_an_unknown_instance(monkeypatch) -> None:  # noqa: ANN001
    """An empty pod scope would read as "this instance had no events"."""
    _patch_job_events(monkeypatch)
    monkeypatch.setattr(
        job_events,
        "list_job_instance_events",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an unknown selector must not reach the events Action")
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "job",
            "events",
            "train-a",
            "--workspace",
            "Test Workspace",
            "--instance",
            "rank=7",
        ],
    )

    assert result.exit_code == 12
    payload = json.loads(result.output)
    assert payload["error"]["type"] == "ValidationError"
    assert "rank=0, rank=1" in payload["error"]["message"]


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
