"""Exercise job-list pagination through the CLI and Browser API decoder."""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from inspire.cli.commands.job import job_commands
from inspire.cli.main import main
from inspire.config import Config
from inspire.platform.web.browser_api import jobs as jobs_api
from inspire.platform.web.session.models import TransientAPIError


@pytest.fixture
def job_list(monkeypatch):
    monkeypatch.setattr(Config, "from_files_and_env", classmethod(
        lambda cls, **kw: (Config(), {}),
    ))
    monkeypatch.setattr(job_commands, "get_web_session", lambda: SimpleNamespace())
    monkeypatch.setattr(job_commands, "_current_user_id", lambda session: "user-test")
    monkeypatch.setattr(job_commands, "_list_workspace_ids", lambda session, workspace: (
        ["ws-one", "ws-two"] if workspace == "all" else ["ws-one"]
    ))
    monkeypatch.setattr(job_commands, "_workspace_name", lambda session, workspace: workspace)

    def invoke(fetch, *, workspace="Training", structured=True, options=("--all",)):
        monkeypatch.setattr(jobs_api, "_request_json", fetch)
        return CliRunner().invoke(main, (["--json"] if structured else []) + [
            "job", "list", "--workspace", workspace, "--keyword", "wave", *options,
        ])

    return invoke


def job(index, workspace="ws-one"):
    return {"job_id": f"job-{workspace}-{index}", "name": f"wave-{index:03d}",
            "status": "job_running", "workspace_id": workspace, "command": "true"}


@pytest.mark.parametrize("workspace", ["Training", "all"])
@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("totals", ["missing", "changing", "overstated"])
def test_all_reaches_empty_page_despite_short_pages_and_unreliable_totals(
    job_list, workspace, structured, totals,
):
    calls = []
    pages = [[job(0)], [job(i) for i in range(1, 101)],
             [job(i) for i in range(101, 201)], [job(i) for i in range(201, 211)], []]

    def fetch(session, method, path, *, body, **kw):
        assert path.endswith("Action=ListJobs")
        assert body["keyword"] == "wave"
        calls.append((body["workspace_id"], body["page_num"]))
        if body["workspace_id"] == "ws-two":
            return {"Result": {"jobs": [], "total": 0}}
        page = body["page_num"]
        payload = {"jobs": pages[page - 1]}
        if totals != "missing":
            payload["total"] = 999 if totals == "overstated" else [1, 211, 1, 211, 211][page - 1]
        return {"Result": payload}

    result = job_list(fetch, workspace=workspace, structured=structured)
    assert result.exit_code == 0, result.output
    if structured:
        data = json.loads(result.output)["data"]
        assert len(data["items"]) == 211
        assert not data.get("truncated", False)
    else:
        assert sum("wave-" in line for line in result.output.splitlines()) == 211
        assert "Showing" not in result.output
    assert [page for ws, page in calls if ws == "ws-one"] == [1, 2, 3, 4, 5]


def test_all_has_no_hidden_fifty_page_limit(job_list):
    def fetch(session, method, path, *, body, **kw):
        page = body["page_num"]
        return {"Result": {"jobs": [job(page)] if page <= 51 else [], "total": 10000}}

    result = job_list(fetch)
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.output)["data"]["items"]) == 51


@pytest.mark.parametrize("failure", ["transient", "malformed", "repeated"])
@pytest.mark.parametrize("workspace", ["Training", "all"])
def test_all_never_reports_partial_scan_as_success(job_list, failure, workspace):
    def fetch(session, method, path, *, body, **kw):
        if body["workspace_id"] == "ws-two":
            return {"Result": {"jobs": [], "total": 0}}
        if body["page_num"] == 1 or failure == "repeated":
            return {"Result": {"jobs": [job(0)], "total": 211}}
        if failure == "transient":
            raise TransientAPIError("temporary service failure")
        return {"Result": {"total": 211}}

    result = job_list(fetch, workspace=workspace)
    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert "data" not in payload


def test_all_deduplicates_shifted_pages_without_collapsing_distinct_same_named_jobs(job_list):
    rows = [job(1), {**job(2), "name": "wave-001"}]

    def fetch(session, method, path, *, body, **kw):
        pages = [[rows[0]], rows, []]
        return {"Result": {"jobs": pages[body["page_num"] - 1], "total": 2}}

    result = job_list(fetch)
    assert result.exit_code == 0, result.output
    assert [row["name"] for row in json.loads(result.output)["data"]["items"]] == [
        "wave-001", "wave-001",
    ]


def test_all_uses_larger_pages_and_stops_at_deadline(job_list, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    calls: list[int] = []
    monkeypatch.setattr(job_commands.time, "monotonic", lambda: now[0])

    def fetch(
        session: Any, method: str, path: str, *, body: dict[str, Any], **kw: Any,
    ) -> dict[str, Any]:
        assert body["page_size"] == 999
        calls.append(body["page_num"])
        now[0] += 31
        return {"Result": {"jobs": [job(body["page_num"])], "total": 100000}}

    result = job_list(fetch)
    assert result.exit_code == 14, result.output
    payload = json.loads(result.output)
    assert payload["error"]["type"] == "TimeoutError"
    assert "--scan-timeout" in payload["error"]["message"]
    assert "data" not in payload
    assert calls == [1, 2]


def test_all_can_extend_scan_budget(job_list, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr(job_commands.time, "monotonic", lambda: now[0])

    def fetch(
        session: Any, method: str, path: str, *, body: dict[str, Any], **kw: Any,
    ) -> dict[str, Any]:
        now[0] += 31
        return {"Result": {"jobs": [job(1)] if body["page_num"] == 1 else [], "total": 1}}

    result = job_list(fetch, options=("--all", "--scan-timeout", "120"))
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.output)["data"]["items"]) == 1
