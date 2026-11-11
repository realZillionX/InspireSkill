"""Partial batch status contracts; shared fixtures forbid real HTTP."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from importlib import import_module

import pytest

from inspire import (
    AuthenticationError, HPCJobRef, InspireAsyncClient, JobRef,
    ResourceNotFoundError, StatusResult, TransportError, ValidationError,
)
from inspire.platform.web.session.models import TransientAPIError
from test_sdk import client as client
from test_sdk_async import tracked as tracked


@pytest.fixture(params=["jobs", "hpc"])
def batch(client, monkeypatch, request):
    kind = request.param
    ref_type = JobRef if kind == "jobs" else HPCJobRef
    module = import_module(
        "inspire.platform.web.browser_api." + ("jobs" if kind == "jobs" else "hpc_jobs")
    )
    refs = [
        ref_type(name, client.account, client.base_url, "opaque-" + name, ws)
        for name, ws in [
            ("running", "ws-test"), ("missing", "ws-test"),
            ("foreign", "ws-test"), ("finished", "ws-second"),
        ]
    ]
    rows = {
        refs[0].key: {"name": "running", "status": "RUNNING", "workspace_id": "ws-test"},
        refs[2].key: {"name": "foreign", "status": "SUCCEEDED", "workspace_id": "ws-wrong"},
        refs[3].key: {"name": "finished", "status": "SUCCEEDED", "workspace_id": "ws-second"},
    }
    calls = []
    failure = []

    def read(keys, *, workspace_id, session):
        calls.append((list(keys), workspace_id))
        if failure and workspace_id == "ws-second":
            raise failure[0]
        return {key: rows[key] for key in reversed(keys) if key in rows}

    monkeypatch.setattr(
        module, "list_jobs_by_ids" if kind == "jobs" else "list_hpc_jobs_by_ids", read
    )
    monkeypatch.setattr(
        module, "get_job_detail_v2" if kind == "jobs" else "get_hpc_job_detail",
        lambda *a, **kw: pytest.fail("unexpected per-item detail request"),
    )
    return kind, refs, rows, calls, failure


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def status(client, tracked, batch, request):
    kind = batch[0]

    def invoke(refs, **kwargs):
        if not request.param:
            return getattr(client, kind).status(refs, **kwargs)

        async def run():
            async with InspireAsyncClient("alpha") as sdk:
                return await getattr(sdk, kind).status(refs, **kwargs)

        return asyncio.run(run())

    return invoke


def test_mixed_results_order_duplicates_and_public_output(batch, status):
    _, refs, _, calls, _ = batch
    inputs = [refs[3], refs[1], refs[0], refs[2], refs[1], refs[0]]
    results = status(inputs, allow_partial=True)
    assert all(isinstance(item, StatusResult) for item in results)
    assert [item.ref for item in results] == inputs
    assert [item.outcome for item in results] == [
        "status", "missing", "status", "error", "missing", "status",
    ]
    assert results[0].value.status == "SUCCEEDED"
    assert results[2].value.status == "RUNNING"
    for i in (1, 3, 4):
        assert results[i].value is None
    assert results[1].error is None
    assert isinstance(results[3].error, ValidationError)
    assert results[2] == results[5]
    assert calls == [
        ([refs[3].key], "ws-second"),
        ([ref.key for ref in inputs if ref.workspace_id == "ws-test"], "ws-test"),
    ]
    public = repr([item.to_dict() for item in results]) + repr(results)
    assert "opaque-" not in public
    assert "ws-wrong" not in public
    assert "ws-test" not in public
    assert results[1].to_dict() == {
        "name": "missing", "outcome": "missing", "value": None, "error": None,
    }


def test_strict_behavior_and_empty_input(batch, status):
    _, refs, _, calls, _ = batch
    assert [item.status for item in status([refs[0], refs[3], refs[0]])] == [
        "RUNNING", "SUCCEEDED", "RUNNING",
    ]
    for kwargs in ({}, {"allow_partial": False}):
        with pytest.raises(ResourceNotFoundError):
            status(refs, **kwargs)
        with pytest.raises(ValidationError, match="workspace"):
            status([refs[2]], **kwargs)
    calls.clear()
    assert status([], allow_partial=True) == ()
    assert calls == []
    assert [item.outcome for item in status([refs[1], refs[1]], allow_partial=True)] == [
        "missing", "missing",
    ]


@pytest.mark.parametrize("failure,expected", [
    (TransportError("network unavailable"), TransportError),
    (AuthenticationError("permission denied"), AuthenticationError),
    (TransientAPIError("rate limited", status=429), TransportError),
])
def test_whole_batch_failures_raise_after_successful_workspace(batch, status, failure, expected):
    _, refs, _, calls, failures = batch
    failures.append(failure)
    with pytest.raises(expected):
        status([refs[0], refs[3]], allow_partial=True)
    assert len(calls) == 2


@pytest.mark.parametrize("invalid", ["account", "base_url", "kind", "string", "workspace"])
def test_invalid_inputs_still_raise_before_batch(batch, status, invalid):
    _, refs, _, calls, _ = batch
    ref = refs[0]
    kwargs = {}
    if invalid == "account":
        ref = replace(ref, account="other")
    elif invalid == "base_url":
        ref = replace(ref, base_url="https://other.invalid")
    elif invalid == "kind":
        ref_type = HPCJobRef if isinstance(ref, JobRef) else JobRef
        ref = ref_type(ref.name, ref.account, ref.base_url, ref.key, ref.workspace_id)
    elif invalid == "workspace":
        from inspire import WorkspaceRef
        kwargs["workspace"] = WorkspaceRef("Other", ref.account, ref.base_url, "ws-other")
    with pytest.raises(ValidationError):
        status("running" if invalid == "string" else [ref], allow_partial=True, **kwargs)
    assert calls == []
