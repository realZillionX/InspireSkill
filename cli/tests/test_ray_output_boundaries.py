from __future__ import annotations

import json
import logging

import pytest
from click.testing import CliRunner

from inspire.cli.commands.ray import ray_commands
from inspire.cli.context import Context
from inspire.cli.main import main as cli_main
from inspire.config import ConfigError


def test_ray_image_lookup_details_only_reach_debug_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    detail = "GET https://internal.invalid/images failed for mirror-12345678"

    def fail_lookup(*_args, **_kwargs):
        raise RuntimeError(detail)

    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "list_images_by_source",
        fail_lookup,
    )
    ctx = Context()
    ctx.debug = True

    with caplog.at_level(logging.DEBUG, logger=ray_commands.__name__):
        with pytest.raises(ConfigError, match="Image 'demo:latest' not found"):
            ray_commands._resolve_image_id(
                "demo:latest", session=object(), ctx=ctx, workspace_id="ws-test"
            )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert detail in caplog.text


def test_ray_events_default_to_twenty_compact_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ray_commands, "get_web_session", lambda: object())
    monkeypatch.setattr(
        ray_commands.Config,
        "from_files_and_env",
        lambda **_kwargs: (object(), []),
    )
    monkeypatch.setattr(
        ray_commands,
        "_run_readonly_ray_operation",
        lambda *_args, **_kwargs: [
            {
                "timestamp": str(index),
                "type": "Warning",
                "reason": "Pending",
                "message": f"event-{index}",
                "object_id": f"rj-{index:08x}",
                "debug": {"drop": True},
            }
            for index in range(35)
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "ray",
            "events",
            "demo-ray",
            "--workspace",
            "CPU资源空间",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    events = data["items"]
    assert len(events) == 20
    assert events[0]["message"] == "event-15"
    assert events[-1]["message"] == "event-34"
    assert data["shown"] == 20
    assert data["total"] == 35
    assert data["truncated"] is True
    assert all(set(event) <= {"time", "type", "reason", "message", "count"} for event in events)
