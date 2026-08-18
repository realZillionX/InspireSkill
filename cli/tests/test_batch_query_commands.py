"""CLI behaviour for the batch reads: `job status`, `hpc status`, `job events`.

The point of these commands is that several names cost one platform request
per twenty jobs instead of one each, and that a name which cannot be answered
is reported next to the ones that could rather than ending the command. Both
halves are pinned here, because both are easy to lose in a refactor that
"simplifies" the batch path back into a loop.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from inspire.cli.commands.hpc import hpc_commands
from inspire.cli.commands.job import job_commands, job_events
from inspire.cli.main import main as cli_main


class _FakeSession:
    def __init__(self) -> None:
        self.workspace_id = "ws-1"


def _record(name: str, job_id: str, status: str = "job_running") -> dict:
    return {
        "job_id": job_id,
        "name": name,
        "status": status,
        "workspace_id": "ws-1",
    }


@pytest.fixture
def batch_job_env(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Resolve names locally and capture the batched detail request."""
    ids = {"run-a": "job-a", "run-b": "job-b", "run-c": "job-c"}
    calls: list[list[str]] = []

    monkeypatch.setattr(job_commands, "get_web_session", lambda: _FakeSession())
    monkeypatch.setattr(
        job_commands, "_list_workspace_ids", lambda session, *, workspace: ["ws-1"]
    )
    monkeypatch.setattr(job_commands, "_close_web_client", lambda: None)

    def _resolve(*, job, **kwargs):  # noqa: ANN001
        if job not in ids:
            raise job_commands.WebJobResolutionError(f"No web job matching {job!r} found.")
        return ids[job]

    monkeypatch.setattr(job_commands, "_resolve_web_job_id", _resolve)

    # `job_events` imports these by name, so the binding it holds is the one
    # that has to be replaced -- patching only `job_commands` leaves the events
    # path talking to the real resolver.
    def _resolve_batch(names, *, workspace):  # noqa: ANN001
        resolved = {name: ids[name] for name in names if name in ids}
        failures = {
            name: f"No web job matching {name!r} found."
            for name in names
            if name not in ids
        }
        return resolved, failures

    monkeypatch.setattr(job_events, "_resolve_batch_job_ids", _resolve_batch)
    monkeypatch.setattr(
        job_events, "_list_workspace_ids", lambda session, *, workspace: ["ws-1"]
    )

    def _list_jobs_by_ids(job_ids, *, workspace_id, session=None):  # noqa: ANN001
        calls.append(list(job_ids))
        by_id = {value: key for key, value in ids.items()}
        return {
            value: _record(by_id[value], value)
            for value in job_ids
            if value in by_id
        }

    monkeypatch.setattr(
        job_commands.browser_api_module, "list_jobs_by_ids", _list_jobs_by_ids
    )
    return {"calls": calls, "ids": ids}


def test_several_names_take_one_batched_request(batch_job_env: dict) -> None:
    result = CliRunner().invoke(
        cli_main,
        ["job", "status", "run-a", "run-b", "run-c", "--workspace", "W"],
    )

    assert result.exit_code == 0, result.output
    assert batch_job_env["calls"] == [["job-a", "job-b", "job-c"]]
    for name in ("run-a", "run-b", "run-c"):
        assert name in result.output


def test_one_bad_name_does_not_sink_the_others(batch_job_env: dict) -> None:
    result = CliRunner().invoke(
        cli_main,
        ["job", "status", "run-a", "nope", "run-b", "--workspace", "W"],
    )

    # The two that resolved are still printed...
    assert "run-a" in result.output
    assert "run-b" in result.output
    # ...the one that did not is named as unresolved, not as an empty status...
    assert "Unresolved: nope" in result.output
    # ...and the request only carried the ids that resolved.
    assert batch_job_env["calls"] == [["job-a", "job-b"]]
    # ...while the exit code still says the answer is partial.
    assert result.exit_code != 0


def test_json_keeps_unresolved_names_out_of_items(batch_job_env: dict) -> None:
    result = CliRunner().invoke(
        cli_main,
        ["--json", "job", "status", "run-a", "nope", "--workspace", "W"],
    )

    payload = json.loads(result.output)["data"]
    assert [item["name"] for item in payload["items"]] == ["run-a"]
    assert [row["name"] for row in payload["unresolved"]] == ["nope"]


def test_a_resolved_id_the_platform_drops_is_reported(
    monkeypatch: pytest.MonkeyPatch, batch_job_env: dict
) -> None:
    """A cached id for a deleted job must not read as a job with no fields."""
    monkeypatch.setattr(
        job_commands.browser_api_module,
        "list_jobs_by_ids",
        lambda job_ids, *, workspace_id, session=None: {},
    )

    result = CliRunner().invoke(
        cli_main, ["job", "status", "run-a", "run-b", "--workspace", "W"]
    )

    assert result.exit_code != 0
    assert "Unresolved: run-a" in result.output
    assert "Unresolved: run-b" in result.output


def test_pick_is_refused_for_several_names(batch_job_env: dict) -> None:
    """--pick chooses among candidates for one name; it cannot mean anything here."""
    result = CliRunner().invoke(
        cli_main,
        ["job", "status", "run-a", "run-b", "--workspace", "W", "--pick", "2"],
    )

    assert result.exit_code != 0
    assert "single name" in result.output


def test_single_name_still_uses_the_detail_action(
    monkeypatch: pytest.MonkeyPatch, batch_job_env: dict
) -> None:
    """One name keeps the stale-handle-retry path and its unchanged output."""
    seen: list[str] = []

    def _detail(job_id, session=None):  # noqa: ANN001
        seen.append(job_id)
        return _record("run-a", job_id)

    monkeypatch.setattr(
        job_commands.browser_api_module, "get_job_detail_v2", _detail
    )
    monkeypatch.setattr(
        job_commands,
        "_run_readonly_web_job_operation",
        lambda *, job, operation, **kwargs: operation("job-a", _FakeSession()),
    )

    result = CliRunner().invoke(cli_main, ["job", "status", "run-a", "--workspace", "W"])

    assert result.exit_code == 0, result.output
    assert seen == ["job-a"]
    assert batch_job_env["calls"] == []


def test_batch_events_label_every_row_with_its_job(
    monkeypatch: pytest.MonkeyPatch, batch_job_env: dict
) -> None:
    monkeypatch.setattr(job_events, "get_web_session", lambda: _FakeSession())
    monkeypatch.setattr(job_events, "_close_web_client", lambda: None)
    monkeypatch.setattr(
        job_events.browser_api_module,
        "list_job_events_by_ids",
        lambda job_ids, session=None: (
            {
                "job-a": [
                    {"object_id": "job-a", "reason": "Unschedulable", "type": "Warning",
                     "last_timestamp": "1786782891000", "message": "no room"}
                ],
                "job-b": [
                    {"object_id": "job-b", "reason": "Scheduled", "type": "Normal",
                     "last_timestamp": "1786782991000", "message": "placed"}
                ],
            },
            [],
        ),
    )

    result = CliRunner().invoke(
        cli_main, ["job", "events", "run-a", "run-b", "--workspace", "W"]
    )

    assert result.exit_code == 0, result.output
    assert "Job" in result.output
    assert "run-a" in result.output and "run-b" in result.output
    assert "Unschedulable" in result.output and "Scheduled" in result.output


def test_batch_events_refuse_instance_selectors(batch_job_env: dict) -> None:
    """Per-pod events need an instance listing per job; that is a single-job query."""
    result = CliRunner().invoke(
        cli_main,
        ["job", "events", "run-a", "run-b", "--workspace", "W", "--instance", "rank=0"],
    )

    assert result.exit_code != 0
    assert "single NAME" in result.output


def test_hpc_batch_status_lists_the_workspace_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N names must not become N listings; that is the whole point of batching."""
    listings: list[str] = []
    batched: list[list[str]] = []

    class _Job:
        def __init__(self, name: str, job_id: str) -> None:
            self.name = name
            self.job_id = job_id
            self.status = "RUNNING"
            self.workspace_id = "ws-1"
            self.created_at = "1"

    monkeypatch.setattr(hpc_commands, "get_web_session", lambda: _FakeSession())
    monkeypatch.setattr(
        hpc_commands, "select_workspace_id", lambda **kwargs: "ws-1"
    )
    monkeypatch.setattr(hpc_commands, "_current_user_id", lambda session: "user-1")

    def _list_hpc_jobs(**kwargs):  # noqa: ANN001
        listings.append("call")
        return ([_Job("solver-a", "hpc-a"), _Job("solver-b", "hpc-b")], 2)

    def _by_ids(job_ids, *, workspace_id, session=None):  # noqa: ANN001
        batched.append(list(job_ids))
        return {
            value: {"job_id": value, "name": f"solver-{value[-1]}", "status": "RUNNING"}
            for value in job_ids
        }

    monkeypatch.setattr(
        hpc_commands.browser_api_module, "list_hpc_jobs", _list_hpc_jobs
    )
    monkeypatch.setattr(
        hpc_commands.browser_api_module, "list_hpc_jobs_by_ids", _by_ids
    )

    result = CliRunner().invoke(
        cli_main, ["hpc", "status", "solver-a", "solver-b", "--workspace", "W"]
    )

    assert result.exit_code == 0, result.output
    assert len(listings) == 1
    assert batched == [["hpc-a", "hpc-b"]]


def test_hpc_batch_status_reports_an_unknown_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hpc_commands, "get_web_session", lambda: _FakeSession())
    monkeypatch.setattr(hpc_commands, "select_workspace_id", lambda **kwargs: "ws-1")
    monkeypatch.setattr(hpc_commands, "_current_user_id", lambda session: "user-1")
    monkeypatch.setattr(
        hpc_commands.browser_api_module,
        "list_hpc_jobs",
        lambda **kwargs: ([], 0),
    )
    monkeypatch.setattr(
        hpc_commands.browser_api_module,
        "list_hpc_jobs_by_ids",
        lambda job_ids, *, workspace_id, session=None: {},
    )

    result = CliRunner().invoke(
        cli_main, ["hpc", "status", "solver-a", "solver-b", "--workspace", "W"]
    )

    assert result.exit_code != 0
    assert "Unresolved: solver-a" in result.output
    assert "Unresolved: solver-b" in result.output
