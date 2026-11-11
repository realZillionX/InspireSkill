"""Offline regression coverage for the PR 102 review paths."""
import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import requests

from test_sdk import client as client, planned as planned
from test_sdk_async import tracked as tracked
from inspire.sdk import (
    InspireAsyncClient, InspireError, ResolutionIncompleteError,
    Resource, SubmissionUncertainError, TransportError, WorkspaceRef,
)
from inspire.sdk.jobs import Jobs
from inspire.sdk.resources import Workspaces
from inspire.platform.web.session import browser_client as browser
from inspire.platform.web.session.envelope import _v2_result


ECHOES = [
    '{"request_body":{"token":"FAKE_SECRET"},"detail":"invalid field"}',
    'invalid field\nAuthorization: Bearer FAKE_SECRET',
    'invalid field\nCookie: session=FAKE_SECRET; other=FAKE_COOKIE',
    '{"Authorization":"Bearer FAKE_SECRET", "detail":"invalid field"}',
    '{"Cookie":"session=FAKE_SECRET; other=FAKE_COOKIE", "detail":"invalid field"}',
    '{"password":"FAKE_SECRET", "client_secret":"FAKE_COOKIE", "detail":"invalid field"}',
]


def assert_safe(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        text = str(error) + repr(vars(error))
        assert "FAKE_SECRET" not in text
        assert "FAKE_COOKIE" not in text
        assert not hasattr(error, "body")
        error = error.__cause__ or error.__context__


def wire(monkeypatch, payload, status, browser_path, sent):
    def response():
        sent.append(1)
        return httpx.Response(status, json=payload, headers={"Retry-After": "0"})

    monkeypatch.setattr(requests.Session, "request", lambda *a, **kw: response())

    async def send(*a, **kw):
        return response()

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    if not browser_path:
        return

    def unavailable(*a, **kw):
        sent.append("http-fallback")
        raise requests.ConnectionError("Use the browser fallback")

    async def async_unavailable(*a, **kw):
        return unavailable()

    monkeypatch.setattr(requests.Session, "request", unavailable)
    monkeypatch.setattr(httpx.AsyncClient, "send", async_unavailable)
    monkeypatch.setattr("inspire.platform.web.session.retry.backoff_delay", lambda *a, **kw: 0)

    def browser_response():
        r = response()
        return SimpleNamespace(status=r.status_code, headers=dict(r.headers), text=lambda: r.text,
                               json=r.json)

    def sync_init(self, session):
        self.session, self._closed = session, False
        self._context = SimpleNamespace(request=SimpleNamespace(post=lambda *a, **kw: browser_response()))

    async def async_enter(self):
        r = browser_response()
        r.text = AsyncMock(return_value=json.dumps(payload))
        r.json = AsyncMock(return_value=payload)
        self._context = SimpleNamespace(request=SimpleNamespace(post=AsyncMock(return_value=r)))
        return self

    monkeypatch.setattr(browser._BrowserRequestClient, "__init__", sync_init)
    monkeypatch.setattr(browser._BrowserRequestClient, "close", lambda self: None)
    monkeypatch.setattr(browser.AsyncBrowserRequestClient, "__aenter__", async_enter)
    monkeypatch.setattr(browser.AsyncBrowserRequestClient, "close", AsyncMock())


def both_errors(client, monkeypatch, invoke, expected=InspireError, browser_path=False):
    client._transport.allow_browser = browser_path
    with pytest.raises(expected) as caught:
        invoke(client)

    async def run():
        async with InspireAsyncClient("alpha", allow_browser=browser_path) as sdk:
            with pytest.raises(expected) as caught:
                await invoke(sdk)
            return caught.value

    return caught.value, asyncio.run(run())


@pytest.mark.parametrize("browser_path", [False, True])
@pytest.mark.parametrize("status,code", [
    (200, "InvalidParameter"), (200, "Throttling"), (400, "InvalidParameter"),
    (429, "Throttling"), (503, "InternalError"),
])
@pytest.mark.parametrize("echo", ECHOES)
def test_echoes_and_metadata_through_real_drivers(client, tracked, monkeypatch, browser_path, status, code, echo):
    payload = {"ResponseMetadata": {
        "Service": "train", "Action": "Read", "RequestId": "review-request",
        "Error": {"Code": code, "Message": echo},
    }}
    sent = []
    wire(monkeypatch, payload, status, browser_path, sent)

    def load(self):
        return _v2_result(self.client._transport.request("POST", "/fake"))

    monkeypatch.setattr(Workspaces, "_all", load)
    errors = both_errors(client, monkeypatch, lambda sdk: sdk.workspaces.list(), browser_path=browser_path)
    for error in errors:
        assert_safe(error)
        assert (error.code, error.service, error.action, error.request_id, error.status, error.retry_after) == (
            code, "train", "Read", "review-request", status, 0,
        )
        if status in (200, 400):
            assert "invalid field" in str(error)
    assert sent.count(1) >= 2
    if browser_path:
        assert sent.count("http-fallback") == 2


def test_catalog_wrapper_fills_metadata_sync_async(client, tracked, monkeypatch):
    ws = Resource("ws", WorkspaceRef("ws", "alpha", client.base_url, "ws-test", "ws-test"))
    monkeypatch.setattr(Workspaces, "get", lambda *a, **kw: ws)

    def listing(**kw):
        raise TransportError("catalog unavailable", status=503, retry_after=120, request_id="catalog-request")

    monkeypatch.setattr("inspire.platform.web.browser_api.list_images_by_source", listing)
    for error in both_errors(client, monkeypatch, lambda sdk: sdk.images.get("image:v1", workspace=ws.ref),
                             ResolutionIncompleteError):
        assert (error.status, error.retry_after, error.request_id) == (503, 120, "catalog-request")


def test_wrapper_never_overrides_metadata(client, tracked, monkeypatch):
    def load(self):
        try:
            raise TransportError("inner", status=503, retry_after=120, request_id="inner")
        except TransportError as cause:
            raise ResolutionIncompleteError("outer", status=400, request_id="outer") from cause

    monkeypatch.setattr(Workspaces, "_all", load)
    for error in both_errors(client, monkeypatch, lambda sdk: sdk.workspaces.list(), ResolutionIncompleteError):
        assert (error.status, error.retry_after, error.request_id) == (400, 120, "outer")


@pytest.mark.parametrize("non_json", [False, True])
def test_uncertain_create_context_and_single_send(client, planned, tracked, monkeypatch, non_json):
    public, plan = client.jobs._plan(planned)
    monkeypatch.setattr(Jobs, "_plan", lambda *a, **kw: (public, plan))
    sent = []

    def response():
        sent.append(1)
        if non_json:
            return httpx.Response(200, text="not json FAKE_SECRET", headers={"Retry-After": "120"})
        return httpx.Response(200, json={"ResponseMetadata": {
            "RequestId": "create-request", "Service": "train", "Action": "Create",
        }, "Result": {}}, headers={"Retry-After": "120"})

    monkeypatch.setattr(requests.Session, "request", lambda *a, **kw: response())

    async def send(*a, **kw):
        return response()

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    for error in both_errors(client, monkeypatch, lambda sdk: sdk.jobs.create(planned, operation_id="once"),
                             SubmissionUncertainError):
        assert_safe(error)
        assert (error.status, error.retry_after, error.operation_id) == (200, 120, "once")
        assert error.request_id == (None if non_json else "create-request")
        assert error.action == (None if non_json else "Create")
    assert len(sent) == 2


def test_status_overloads_with_mypy(tmp_path):
    snippet = tmp_path / "status_types.py"
    lines = ["from typing_extensions import assert_type",  # already a mypy dependency
             "from inspire.sdk import InspireClient, InspireAsyncClient, Job, HPCJob, StatusResult",
             "async def check(sync: InspireClient, async_: InspireAsyncClient, flag: bool) -> None:"]
    for owner, await_ in [("sync", ""), ("async_", "await ")]:
        for facade, model in [("jobs", "Job"), ("hpc", "HPCJob")]:
            for kw, result in [("", f"tuple[{model}, ...]"), (", allow_partial=False", f"tuple[{model}, ...]"),
                               (", allow_partial=True", f"tuple[StatusResult[{model}], ...]"),
                               (", allow_partial=flag", f"tuple[{model}, ...] | tuple[StatusResult[{model}], ...]")]:
                lines.append(f"    assert_type({await_}{owner}.{facade}.status([]{kw}), {result})")
            lines.append(f"    ({await_}{owner}.{facade}.status([]))[0].status")
    snippet.write_text("\n".join(lines) + "\n")
    result = subprocess.run([sys.executable, "-m", "mypy", "--follow-imports=normal", str(snippet)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
