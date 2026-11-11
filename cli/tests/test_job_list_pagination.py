"""Exercise job-list pagination through the CLI and Browser API decoder."""

import json
from types import SimpleNamespace
from threading import Barrier, Lock
import time

import pytest
from click.testing import CliRunner

from inspire.cli.commands.job import job_commands
from inspire.cli.main import main
from inspire.config import Config
from inspire.platform.web.browser_api import jobs as jobs_api
from inspire.platform.web.session.models import TransientAPIError


@pytest.fixture
def job_list(monkeypatch):
    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(
            lambda cls, **kw: (Config(), {}),
        ),
    )
    monkeypatch.setattr(job_commands, "get_web_session", lambda: SimpleNamespace())
    monkeypatch.setattr(job_commands, "_current_user_id", lambda session: "user-test")
    monkeypatch.setattr(
        job_commands,
        "_list_workspace_ids",
        lambda session, workspace: ["ws-one", "ws-two"] if workspace == "all" else ["ws-one"],
    )
    monkeypatch.setattr(job_commands, "_workspace_name", lambda session, workspace: workspace)

    def invoke(fetch, *, workspace="Training", structured=True, options=("--all",)):
        monkeypatch.setattr(jobs_api, "_request_json", fetch)
        return CliRunner().invoke(
            main,
            (["--json"] if structured else [])
            + [
                "job",
                "list",
                "--workspace",
                workspace,
                "--keyword",
                "wave",
                *options,
            ],
        )

    return invoke


def job(index, workspace="ws-one"):
    return {
        "job_id": f"job-{workspace}-{index}",
        "name": f"wave-{index:03d}",
        "status": "job_running",
        "workspace_id": workspace,
        "command": "true",
    }


@pytest.mark.parametrize("total", [0, 1, 731545, None])
@pytest.mark.parametrize("workspace", ["Training", "all"])
def test_all_stops_on_short_page_ignoring_total(job_list, total, workspace):
    calls = []

    def fetch(session, method, path, *, body, **kw):
        calls.append((body["workspace_id"], body["page_num"]))
        size = body["page_size"]
        rows = (
            ([job(i) for i in range(size)] if body["page_num"] == 1 else [job(size)])
            if body["workspace_id"] == "ws-one"
            else []
        )
        return {"Result": {"jobs": rows, "total": total}}

    result = job_list(fetch, workspace=workspace)
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert len(data["items"]) == 1000
    assert data["truncated"] is False
    assert data["total"] == 1000
    assert [page for ws, page in calls if ws == "ws-one"] == [1, 2]


@pytest.mark.parametrize("structured", [True, False])
def test_full_window_is_explicitly_truncated_without_out_of_window_request(job_list, structured):
    calls = []

    def fetch(session, method, path, *, body, **kw):
        size, page = body["page_size"], body["page_num"]
        assert size * page <= 5000
        calls.append(page)
        return {
            "Result": {"jobs": [job(i) for i in range((page - 1) * size, page * size)], "total": 1}
        }

    result = job_list(fetch, structured=structured)
    assert result.exit_code == 0, result.output
    assert calls == [1, 2, 3, 4, 5]
    assert "truncated" in result.stderr
    if structured:
        data = json.loads(result.stdout)["data"]
        assert len(data["items"]) == 4995
        assert data["total"] is None
        assert data["truncated"] is True
        assert data["incomplete_slices"][0]["reason"] == "paging_window"


@pytest.mark.parametrize("failure", ["transient", "malformed"])
def test_failed_requests_do_not_become_complete_results(job_list, failure):
    def fetch(session, method, path, *, body, **kw):
        if body["page_num"] == 1:
            return {"Result": {"jobs": [job(i) for i in range(body["page_size"])], "total": 0}}
        if failure == "transient":
            raise TransientAPIError("temporary service failure")
        return {"Result": {"total": 211}}

    result = job_list(fetch)
    assert result.exit_code != 0
    assert json.loads(result.output)["success"] is False


def test_repeated_full_page_returns_explicit_partial_result(job_list):
    def fetch(session, method, path, *, body, **kw):
        return {"Result": {"jobs": [job(i) for i in range(body["page_size"])], "total": 0}}

    result = job_list(fetch)
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["truncated"]
    assert data["incomplete_slices"][0]["reason"] == "no_progress"


def test_workspace_requests_overlap_with_bounded_pool(job_list, monkeypatch):
    workspaces = [f"ws-{i}" for i in range(16)]
    monkeypatch.setattr(job_commands, "_list_workspace_ids", lambda *a, **kw: workspaces)
    barrier = Barrier(8, timeout=5)
    lock = Lock()
    active = 0
    peak = 0

    def fetch(session, method, path, *, body, **kw):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait()
        time.sleep(0.01)
        with lock:
            active -= 1
        return {"Result": {"jobs": [], "total": 731545}}

    result = job_list(fetch, workspace="all")
    assert result.exit_code == 0, result.output
    assert peak == 8


@pytest.mark.parametrize(
    "options", [("--all", "--status", "RUNNING"), ("--all", "--active"), ("--limit", "2")]
)
def test_filters_and_bounded_lists_use_short_page_termination(job_list, options):
    calls = []

    def fetch(session, method, path, *, body, **kw):
        assert body["keyword"] == "wave"
        assert body["page_num"] == 1
        calls.append(body)
        return {"Result": {"jobs": [job(1)], "total": 731545}}

    result = job_list(fetch, options=options)
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout)["data"]["items"]) == 1
    assert calls


def test_watch_uses_shared_scan(job_list, monkeypatch):
    calls = []

    def fetch(session, method, path, *, body, **kw):
        calls.append(body)
        assert body["page_num"] == 1
        return {"Result": {"jobs": [], "total": 731545}}

    def stop(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(job_commands.time, "sleep", stop)
    result = job_list(fetch, structured=False, options=("--watch",))
    assert result.exit_code == 0, result.output
    assert len(calls) == 1


def test_shifted_pages_deduplicate_ids_but_keep_distinct_same_named_jobs(job_list, monkeypatch):
    monkeypatch.setattr(job_commands, "_job_list_page_size", lambda _: 2)
    rows = [job(1), {**job(2), "name": "wave-001"}, job(3)]
    calls = []

    def fetch(session, method, path, *, body, **kw):
        page = body["page_num"]
        calls.append(page)
        return {"Result": {"jobs": [rows[:2], rows[1:], []][page - 1], "total": 1}}

    result = job_list(fetch)
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert [row["name"] for row in data["items"]] == ["wave-001", "wave-001", "wave-003"]
    assert data["truncated"] is False
    assert calls == [1, 2, 3]
