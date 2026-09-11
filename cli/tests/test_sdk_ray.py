"""Ray SDK payload, writes, observations and elastic worker contracts."""

from __future__ import annotations
import pytest
from test_sdk import client as client
from test_sdk_hpc import (
    catalog as catalog,
    check_cli_payload,
    check_write,
    check_wait,
    check_logs,
    check_discovery,
)
from inspire import RayJobRef, ValidationError
from inspire.platform.web.browser_api import ray_jobs as api


def test_fully_populated_ray_payload_equals_cli_dry_run(client, catalog, monkeypatch):
    check_cli_payload("ray", client, catalog, monkeypatch)


@pytest.mark.parametrize("action", ["create", "start", "stop", "delete"])
@pytest.mark.parametrize("outcome", ["ok", "timeout", "platform"])
def test_ray_writes_single_dispatch(client, catalog, monkeypatch, action, outcome):
    check_write("ray", action, outcome, client, catalog, monkeypatch)


def test_ray_missing_create_identity_is_uncertain(client, catalog, monkeypatch):
    check_write("ray", "create", "missing", client, catalog, monkeypatch)


def test_ray_wait_vocabulary(client, monkeypatch):
    check_wait("ray", client, monkeypatch)


@pytest.mark.parametrize("head", [False, True])
def test_ray_logs_and_instances_match_cli(client, monkeypatch, head):
    check_logs("ray", client, monkeypatch, head)


def test_ray_discovery_quota_and_identity(client, catalog, monkeypatch):
    check_discovery("ray", client, catalog, monkeypatch)


def test_ray_scaling_views(client, monkeypatch):
    from inspire.cli.commands.ray.ray_scaling import _public_ray_scaling_events

    rows = [
        {
            "event_time": "1770000001000",
            "event_type": "scale_up",
            "replicas_before": "1",
            "replicas_after": "3",
        },
        {"event_time": "1770000000000", "event_type": "initialized", "replicas_after": 1},
    ]
    calls = []

    def fetch(*a, **kw):
        calls.append(kw)
        return rows, 2

    monkeypatch.setattr(api, "list_ray_job_scaling_histories", fetch)
    ref = RayJobRef("example", client.account, client.base_url, "key", "ws-test")
    assert tuple(row.to_dict() for row in client.ray.scaling(ref, group="decode", limit=1)) == tuple(
        _public_ray_scaling_events(rows[:1], group="decode")
    )
    assert calls[0]["worker_group_name"] == "decode" and calls[0]["page_size"] == -1


@pytest.mark.parametrize(
    "worker", ["oops", "name=x", "name=x;image=i;group=g;quota=0,4,16;min=3;max=1"]
)
def test_worker_error_text_matches_cli(client, catalog, monkeypatch, worker):
    from dataclasses import replace
    from inspire.cli.commands.ray.ray_commands import _parse_worker_spec

    with pytest.raises(ValueError) as sdk:
        client.ray.plan(replace(catalog.ray, workers=[worker]))
    with pytest.raises(Exception) as cli:
        _parse_worker_spec(worker)
    assert str(sdk.value) == str(cli.value)
    assert isinstance(sdk.value, ValidationError)


def test_ray_event_filters_follow_and_metrics(client, monkeypatch):
    from test_sdk_hpc import check_events_metrics

    check_events_metrics("ray", client, monkeypatch)


def test_ray_plan_failure_does_not_dispatch(client, catalog, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(
        client._transport, "_once", lambda *a, **kw: pytest.fail("invalid plan sent")
    )
    with pytest.raises(ValidationError, match="At least one"):
        client.ray.create(replace(catalog.ray, workers=[]))


def test_ray_read_error_keeps_platform_text(client, monkeypatch):
    ref = RayJobRef("example", client.account, client.base_url, "key", "ws-test")

    def fail(*a, **kw):
        raise ValueError("平台明文错误")

    monkeypatch.setattr(api, "get_ray_job_detail", fail)
    with pytest.raises(ValidationError, match="平台明文错误"):
        client.ray.get(ref)


def test_ray_spec_defaults_match_cli_options():
    from dataclasses import fields
    from inspire.cli.main import main as cli
    from inspire import RayJobCreateSpec

    options = {option.name: option.default for option in cli.commands["ray"].commands["create"].params}
    defaults = {item.name: item.default for item in fields(RayJobCreateSpec)}
    for name in ['image_type', 'priority', 'public_path_readonly', 'shm_gib', 'description']:
        option_name = "shm_size" if name == "shm_gib" else name
        assert (defaults[name] if defaults[name] is not None else "") == (
            options[option_name] if options[option_name] is not None else ""
        ), name
