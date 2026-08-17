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


def _ray_pod(role: str, kind: str, suffix: str) -> dict:
    return {
        "instance_id": f"rj-df33bdba-x66vw-{suffix}",
        "name": f"rj-df33bdba-x66vw-{suffix}",
        "instance_type": kind,
        "worker_group_name": role,
        "cpu_count": 1,
        "memory_size": 4,
        "gpu_count": 0,
    }


_RAY_PODS = [
    _ray_pod("", "head", "head-825s5"),
    _ray_pod("w", "worker", "w-worker-77hmw"),
]


def _ray_event(object_id: str, object_type: str, reason: str, last: str, event_id: str) -> dict:
    return {
        "count": 1,
        "first_timestamp": last,
        "id": event_id,
        "last_timestamp": last,
        "message": f"{reason} happened",
        "object_id": object_id,
        "object_type": object_type,
        "reason": reason,
        "source_component": "kubelet",
        "type": "Normal",
    }


def _patch_ray_events(monkeypatch, events: list[dict]) -> list[dict]:  # noqa: ANN001
    sent: list[dict] = []
    monkeypatch.setattr(ray_commands, "get_web_session", lambda: object())
    monkeypatch.setattr(
        ray_commands.Config,
        "from_files_and_env",
        lambda **_kwargs: (object(), []),
    )
    monkeypatch.setattr(
        ray_commands,
        "_run_readonly_ray_operation",
        lambda _ctx, **kwargs: kwargs["operation"]("ray-internal", object()),
    )
    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "list_ray_job_instances",
        lambda _job_id, **_kwargs: (list(_RAY_PODS), len(_RAY_PODS)),
    )

    def _fake_events(_job_id, **kwargs):  # noqa: ANN001
        sent.append(kwargs)
        pods = kwargs.get("pod_names")
        if pods is None:
            return list(events)
        return [event for event in events if event["object_id"] in set(pods)]

    monkeypatch.setattr(ray_commands.browser_api_module, "list_ray_job_events", _fake_events)
    return sent


def test_ray_events_carry_cluster_and_pod_rows_in_one_timeline(monkeypatch) -> None:  # noqa: ANN001
    """One call already answers both levels; only the labels were missing."""
    sent = _patch_ray_events(
        monkeypatch,
        [
            _ray_event("rj-df33bdba", "job", "CreatedRayCluster", "3", "575463"),
            _ray_event("rj-df33bdba-x66vw-head-825s5", "instance", "Started", "1", "575499"),
            _ray_event("rj-df33bdba-x66vw-w-worker-77hmw", "instance", "Started", "2", "575508"),
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "ray", "events", "demo-ray", "--workspace", "CPU资源空间"],
    )

    assert result.exit_code == 0, result.output
    assert sent[0]["pod_names"] is None
    items = json.loads(result.output)["data"]["items"]
    assert [item.get("instance") for item in items] == [None, "head", "w"] or [
        item.get("instance") for item in items
    ] == ["head", "w", None]
    assert {item["reason"] for item in items} == {
        "CreatedRayCluster",
        "Started",
    }


def test_ray_events_narrow_to_one_role(monkeypatch) -> None:  # noqa: ANN001
    sent = _patch_ray_events(
        monkeypatch,
        [
            _ray_event("rj-df33bdba", "job", "CreatedRayCluster", "3", "575463"),
            _ray_event("rj-df33bdba-x66vw-head-825s5", "instance", "Started", "1", "575499"),
            _ray_event("rj-df33bdba-x66vw-w-worker-77hmw", "instance", "Started", "2", "575508"),
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
            "--instance",
            "head",
        ],
    )

    assert result.exit_code == 0, result.output
    assert sent[0]["pod_names"] == ["rj-df33bdba-x66vw-head-825s5"]
    items = json.loads(result.output)["data"]["items"]
    assert [item["instance"] for item in items] == ["head"]


def test_ray_events_order_a_same_second_burst_causally(monkeypatch) -> None:  # noqa: ANN001
    """Timestamps are per-second; the platform's tie order depends on the filter."""
    _patch_ray_events(
        monkeypatch,
        [
            _ray_event("rj-df33bdba-x66vw-head-825s5", "instance", "Started", "1", "575499"),
            _ray_event("rj-df33bdba-x66vw-head-825s5", "instance", "Pulled", "1", "575493"),
            _ray_event("rj-df33bdba-x66vw-head-825s5", "instance", "Created", "1", "575496"),
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "ray", "events", "demo-ray", "--workspace", "CPU资源空间"],
    )

    assert result.exit_code == 0, result.output
    items = json.loads(result.output)["data"]["items"]
    assert [item["reason"] for item in items] == ["Pulled", "Created", "Started"]


def test_ray_events_reject_an_unknown_instance(monkeypatch) -> None:  # noqa: ANN001
    _patch_ray_events(monkeypatch, [])

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "ray",
            "events",
            "demo-ray",
            "--workspace",
            "CPU资源空间",
            "--instance",
            "no-such-role",
        ],
    )

    assert result.exit_code == 12
    assert json.loads(result.output)["error"]["type"] == "ValidationError"


def test_ray_workload_level_splits_the_single_call_client_side(monkeypatch) -> None:  # noqa: ANN001
    """One call returns both levels, so the cluster view costs no extra request."""
    sent = _patch_ray_events(
        monkeypatch,
        [
            _ray_event("rj-df33bdba", "job", "CreatedRayCluster", "3", "575463"),
            _ray_event("rj-df33bdba-x66vw-head-825s5", "instance", "Started", "1", "575499"),
        ],
    )
    monkeypatch.setattr(
        ray_commands.browser_api_module,
        "list_ray_job_instances",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("--workload-level must not enumerate instances")
        ),
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
            "--workload-level",
        ],
    )

    assert result.exit_code == 0, result.output
    assert sent[0].get("pod_names") is None
    items = json.loads(result.output)["data"]["items"]
    assert [item["reason"] for item in items] == ["CreatedRayCluster"]


def test_ray_workload_level_and_instance_contradict_each_other(monkeypatch) -> None:  # noqa: ANN001
    _patch_ray_events(monkeypatch, [])

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "ray",
            "events",
            "demo-ray",
            "--workspace",
            "CPU资源空间",
            "--workload-level",
            "--instance",
            "head",
        ],
    )

    assert result.exit_code == 12
    assert json.loads(result.output)["error"]["type"] == "InvalidUsage"
