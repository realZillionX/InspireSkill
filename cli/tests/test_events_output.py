"""Tests for compact, Name-only workload event output."""

from __future__ import annotations

import json

import click
from click.testing import CliRunner

from inspire.cli.utils.events import emit_events, render_events_table

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
        "resource": "job",
        "name": "train",
        "count": 1,
        "events": [
            {
                "time": "recent",
                "type": "Warning",
                "reason": "FailedScheduling",
                "message": "Could not schedule <job-id>",
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
