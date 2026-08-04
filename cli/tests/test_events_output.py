"""Tests for compact, Name-only workload event output."""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from inspire.cli.commands.notebook import notebook_lookup as notebook_lookup_module
from inspire.cli.context import Context, EXIT_API_ERROR
from inspire.cli.main import main as cli_main
from inspire.cli.utils.events import (
    _RecentEventKeys,
    emit_events,
    filter_events,
    render_events_table,
    run_events_command,
)

_RAW_JOB_ID = "job-12345678-1234-1234-1234-123456789abc"
notebook_events_module = importlib.import_module(
    "inspire.cli.commands.notebook.notebook_events"
)
notebook_cli_module = importlib.import_module("inspire.cli.utils.notebook_cli")
workspace_module = importlib.import_module("inspire.config.workspaces")


def _event() -> dict:
    return {
        "object_id": _RAW_JOB_ID,
        "object_type": "TrainJob",
        "type": "Warning",
        "reason": "FailedScheduling",
        "message": f"Could not schedule {_RAW_JOB_ID}",
        "from": "scheduler",
        "first_timestamp": "earlier",
        "last_timestamp": "recent",
        "count": 3,
        "source": "web",
        "raw": {"request": "drop"},
        "result": {"debug": True},
    }


def test_events_json_projects_only_public_diagnostics() -> None:
    @click.command()
    def command() -> None:
        emit_events(
            ctx_json=True,
            local_json=False,
            resource_type="job",
            resource_name="train",
            events=[_event()],
        )

    result = CliRunner().invoke(command)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"] == {
        "name": "train",
        "events": [
            {
                "time": "recent",
                "type": "Warning",
                "reason": "FailedScheduling",
                "message": "Could not schedule <redacted>",
                "count": 3,
            }
        ],
    }
    assert _RAW_JOB_ID not in result.output
    event = payload["data"]["events"][0]
    for field in ("object_id", "object_type", "source", "raw", "result", "from"):
        assert field not in event


def test_events_human_output_is_compact_and_scrubs_ids() -> None:
    @click.command()
    def command() -> None:
        render_events_table([_event()])

    result = CliRunner().invoke(command)

    assert result.exit_code == 0, result.output
    assert "Time" in result.output
    assert "Type" in result.output
    assert "Reason" in result.output
    assert "Count" in result.output
    assert "Message" in result.output
    assert "scheduler" not in result.output
    assert _RAW_JOB_ID not in result.output
    assert "object_id" not in result.output


def test_event_messages_scrub_paths_and_url_credentials() -> None:
    from inspire.cli.utils.events import public_event

    public = public_event(
        {
            "reason": "FailedScheduling",
            "message": (
                "see /Users/alice/private/log.txt and "
                "https://user:secret@example.com/events?token=abc"
            ),
        }
    )

    message = str(public["message"])
    assert "/Users/alice/private/log.txt" not in message
    assert "secret" not in message
    assert "?token=abc" not in message
    assert "example.com/events" in message


def test_empty_events_output_has_no_platform_lifecycle_explanation() -> None:
    @click.command()
    def command() -> None:
        render_events_table([])

    result = CliRunner().invoke(command)

    assert result.exit_code == 0
    assert result.output == "(no events)\n"


def test_events_json_fetch_failure_is_one_actionable_error() -> None:
    @click.command()
    def command() -> None:
        ctx = Context()
        ctx.json_output = True
        run_events_command(
            ctx,
            resource_id="internal",
            resource_type="job",
            resource_name="train",
            fetch=lambda: (_ for _ in ()).throw(
                RuntimeError("request failed for job-12345678-1234-1234-1234-123456789abc")
            ),
            json_output_local=False,
            type_filter=None,
            reason_filter=None,
        )

    result = CliRunner().invoke(command)

    assert result.exit_code == EXIT_API_ERROR
    assert result.output.count("\n") == 1
    assert json.loads(result.output) == {
        "success": False,
        "error": {
            "type": "APIError",
            "code": EXIT_API_ERROR,
            "message": "Could not fetch events: request failed for <redacted>",
        },
    }


def test_events_default_output_is_bounded() -> None:
    @click.command()
    def command() -> None:
        ctx = Context()
        ctx.json_output = True
        run_events_command(
            ctx,
            resource_id="internal",
            resource_type="job",
            resource_name="train",
            fetch=lambda: [
                {"message": f"event-{index}", "timestamp": str(index)}
                for index in range(45)
            ],
            json_output_local=False,
            type_filter=None,
            reason_filter=None,
        )

    result = CliRunner().invoke(command)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    events = payload["data"]["events"]
    assert len(events) == 20
    assert events[0]["message"] == "event-25"
    assert events[-1]["message"] == "event-44"


def test_events_explicit_tail_preserves_requested_window_and_content() -> None:
    events = [
        {"message": f"event-{index}", "content": f"raw-{index}"}
        for index in range(45)
    ]

    filtered = filter_events(events, tail=30)

    assert len(filtered) == 30
    assert filtered[0]["message"] == "event-15"
    assert filtered[-1] == {
        "message": "event-44",
        "content": "raw-44",
    }


def test_events_command_explicit_tail_can_exceed_default() -> None:
    @click.command()
    def command() -> None:
        ctx = Context()
        ctx.json_output = True
        run_events_command(
            ctx,
            resource_id="internal",
            resource_type="job",
            resource_name="train",
            fetch=lambda: [
                {"message": f"event-{index}", "timestamp": str(index)}
                for index in range(45)
            ],
            json_output_local=False,
            type_filter=None,
            reason_filter=None,
            tail=30,
        )

    result = CliRunner().invoke(command)

    assert result.exit_code == 0, result.output
    events = json.loads(result.output)["data"]["events"]
    assert len(events) == 30
    assert events[0]["message"] == "event-15"


def test_follow_event_deduplication_window_is_bounded() -> None:
    seen = _RecentEventKeys(limit=2)
    first = {"message": "first"}
    second = {"message": "second"}
    third = {"message": "third"}

    assert seen.remember(first) is True
    assert seen.remember(second) is True
    assert seen.remember(second) is False
    assert seen.remember(third) is True
    assert len(seen) == 2
    assert seen.remember(first) is True
    assert len(seen) == 2


def test_notebook_events_fetch_runs_through_stale_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace()
    seen: dict[str, object] = {}
    fetched_handles: list[str] = []

    monkeypatch.setattr(
        notebook_cli_module,
        "require_web_session",
        lambda *_args, **_kwargs: session,
    )
    monkeypatch.setattr(notebook_cli_module, "load_config", lambda _ctx: SimpleNamespace())
    monkeypatch.setattr(notebook_cli_module, "get_base_url", lambda: "https://example.invalid")
    monkeypatch.setattr(
        workspace_module,
        "resolve_workspace_query_scope",
        lambda *_args, **_kwargs: (["ws-live"], "ws-live"),
    )

    def fake_retry(*_args, operation, **kwargs):  # noqa: ANN001
        seen.update(kwargs)
        return operation("notebook-live"), "notebook-live", "ws-live"

    monkeypatch.setattr(
        notebook_lookup_module,
        "_run_notebook_operation_with_stale_handle_retry",
        fake_retry,
    )
    monkeypatch.setattr(
        notebook_events_module,
        "list_notebook_events",
        lambda notebook_id, **_kwargs: fetched_handles.append(notebook_id)
        or [{"message": "ready", "timestamp": "now"}],
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "notebook",
            "events",
            "demo-notebook",
            "--workspace",
            "CPU资源空间",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen["identifier"] == "demo-notebook"
    assert seen["workspace_ids"] == ["ws-live"]
    assert fetched_handles == ["notebook-live"]
    assert "notebook-live" not in result.output
