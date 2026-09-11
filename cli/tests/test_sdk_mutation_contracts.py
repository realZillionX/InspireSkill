"""Offline regression contracts for the Phase Q mutation audit."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_sdk import client as client

from inspire import (
    AuthenticationError, HPCJobRef, JobRef, Resource, ResourceNotFoundError,
    TransportError, WorkspaceRef,
)
from inspire.platform.web.session.models import TransientAPIError, WebSession
from inspire.platform.web.transport import _SingleSendViolation
from inspire.sdk.cache import CatalogCache
from inspire.sdk.resources import Service, exact


@pytest.mark.parametrize("through_facade", [False, True], ids=["exact", "workspace-get"])
def test_strict_substring_is_not_a_resource_name(client, monkeypatch, through_facade):
    row = Resource("Training Workspace", WorkspaceRef(
        "Training Workspace", client.account, client.base_url, "ws-test", "ws-test",
    ))
    monkeypatch.setattr(client.workspaces, "_all", lambda: [row])
    with pytest.raises(ResourceNotFoundError, match="No resource matches"):
        if through_facade:
            client.workspaces.get("  WORKSPACE  ")
        else:
            exact([row], "  WORKSPACE  ", WorkspaceRef, client)


@pytest.mark.parametrize("kind,ref_cls", [("jobs", JobRef), ("hpc", HPCJobRef)])
def test_get_preserves_distinct_model_timestamps(client, monkeypatch, kind, ref_cls):
    payload = {
        "job_id": "job-test", "name": "training", "status": "SUCCEEDED",
        "workspace_id": "ws-test", "created_at": "2026-09-01T10:00:00Z",
        "finished_at": "2026-09-02T12:34:56Z",
    }
    service = getattr(client, kind)
    if kind == "jobs":
        monkeypatch.setattr(
            "inspire.platform.web.browser_api.jobs.get_job_detail_v2", lambda *a, **kw: payload,
        )
    else:
        monkeypatch.setattr(service, "_binding", replace(
            service._binding, get_detail=lambda *a, **kw: payload,
        ))
    ref = ref_cls("training", client.account, client.base_url, "job-test", "ws-test")
    job = service.get(ref)
    assert job.created_at == "2026-09-01T10:00:00Z"
    assert job.finished_at == "2026-09-02T12:34:56Z"


def test_sdk_sustained_transient_stops_after_three_attempts(client, monkeypatch):
    send = Mock(side_effect=TransientAPIError("persistent rate limit"))
    sleep = Mock()
    monkeypatch.setattr(client._transport, "_once", send)
    monkeypatch.setattr("inspire.platform.web.transport.time.sleep", sleep)
    with pytest.raises(TransportError, match="persistent rate limit"):
        client._transport.request("GET", "/fake")
    assert send.call_count == 3
    assert sleep.call_count == 2


@pytest.mark.parametrize("mismatch", [{"account": "beta"}, {"base_url": "https://other.invalid"}],
                         ids=["account", "base-url"])
def test_cached_session_identity_must_match(client, monkeypatch, mismatch):
    cached = replace(client._transport.session, **mismatch)
    client._transport._session = None
    monkeypatch.setattr(WebSession, "load", lambda **kw: cached)
    with pytest.raises(AuthenticationError, match="No matching cached session; initialize this account first"):
        _ = client._transport.session
    assert client._transport._session is None


def test_nested_single_send_is_refused(client):
    with client._transport.single_send("outer", create=True):
        outer = client._transport._write
        with pytest.raises(_SingleSendViolation, match="single_send blocks cannot be nested"):
            with client._transport.single_send("inner"):
                pass
        assert client._transport._write is outer
    assert client._transport._write is None


def test_catalog_account_keys_and_invalidation_are_isolated():
    cache = CatalogCache()
    alpha = Service(SimpleNamespace(account="alpha", base_url="https://example.invalid", cache=cache))
    beta = Service(SimpleNamespace(account="beta", base_url="https://example.invalid", cache=cache))
    load_alpha = Mock(return_value=[{"image_id": "image-alpha"}])
    load_beta = Mock(return_value=[{"image_id": "image-beta"}])
    assert alpha._catalog("images", ("ws",), load_alpha) == [{"image_id": "image-alpha"}]
    assert beta._catalog("images", ("ws",), load_beta) == [{"image_id": "image-beta"}]
    assert cache.stats()["entries"] == 2
    cache._invalidate("images", "alpha", "https://example.invalid", "ws")
    assert cache.stats()["entries"] == 1
    assert beta._catalog("images", ("ws",), load_beta) == [{"image_id": "image-beta"}]
    load_beta.assert_called_once_with()
    load_alpha.return_value = [{"image_id": "image-alpha-new"}]
    assert alpha._catalog("images", ("ws",), load_alpha) == [{"image_id": "image-alpha-new"}]
    assert load_alpha.call_count == 2
