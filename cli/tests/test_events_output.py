"""Tests for compact, Name-only workload event output."""

from __future__ import annotations

import json

import click
from click.testing import CliRunner

from inspire.cli.context import Context, EXIT_API_ERROR
from inspire.cli.utils.events import emit_events, render_events_table, run_events_command

_RAW_JOB_ID = "job-12345678-1234-1234-1234-123456789abc"


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
