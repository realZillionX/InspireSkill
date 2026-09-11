"""Async resource lifetime contracts with isolated backends."""
from __future__ import annotations

import asyncio

import httpx
import pytest
import requests

from inspire.sdk import InspireAsyncClient, ClientClosedError
from inspire.sdk.resources import Workspaces
from inspire.platform.web.plaza import core as plaza_core
from test_sdk import client as client
from test_sdk_async import tracked as tracked

pytestmark = pytest.mark.timeout(30, method="thread")


@pytest.fixture
def counted_sign_in(monkeypatch):
    signins = []

    class FakePlaza:
        def __init__(self):
            self.http = requests.Session()
            self.user_id = "u"

        def close(self):
            self.http.close()

    def sign_in(session, transport, timeout=30):
        signins.append(session)
        return FakePlaza()

    sign_in.__workflow__ = None
    monkeypatch.setattr(plaza_core, "sign_in", sign_in)
    monkeypatch.setattr(plaza_core, "unwrap", lambda response: {"list": [], "total": 0})

    def send_sync(self, request, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"code":0,"data":{"list":[],"total":0}}'
        response.url = request.url
        response.request = request
        return response

    async def send_async(self, request, **kwargs):
        return httpx.Response(200, json={"code": 0, "data": {"list": [], "total": 0}},
                              request=request)

    monkeypatch.setattr(requests.sessions.Session, "send", send_sync)
    monkeypatch.setattr(httpx.AsyncClient, "send", send_async)
    return signins


def test_sync_client_signs_into_the_plaza_once(client, counted_sign_in):
    for _ in range(4):
        client.datasets.tags()
    assert len(counted_sign_in) == 1, f"sync signed in {len(counted_sign_in)} times"


def test_async_client_signs_into_the_plaza_once(tracked, counted_sign_in):
    async def run():
        async with InspireAsyncClient("alpha") as c:
            for _ in range(4):
                await c.datasets.tags()
            await asyncio.gather(*(c.datasets.tags() for _ in range(4)))

    asyncio.run(run())
    assert len(counted_sign_in) == 1, f"async signed in {len(counted_sign_in)} times"


def test_operation_releases_only_its_view(client, tracked, counted_sign_in, monkeypatch):
    sessions = []
    views = []
    original_close = requests.Session.close

    def close(session):
        sessions.append(session)
        original_close(session)

    def rows(self):
        views.append(self.client)
        self.client._transport.request("GET", "/fake")
        return []

    monkeypatch.setattr(requests.Session, "close", close)
    monkeypatch.setattr(Workspaces, "_all", rows)

    async def run():
        c = InspireAsyncClient("alpha")
        async with c:
            owner = c._client._transport
            # The tracking fixture binds check to the owner; use the real view guard.
            del owner.check
            http = owner._http = requests.Session()
            await c.workspaces.list()
            assert http is not None and http not in sessions
            with pytest.raises(ClientClosedError):
                views[0].__enter__()
            await c.datasets.tags()
            plaza = owner._plaza_slot.client
            assert plaza is not None and plaza.http not in sessions
            await c.workspaces.list()
            await c.datasets.tags()
            assert owner._http is http
            assert owner._plaza_slot.client is plaza
            assert http not in sessions and plaza.http not in sessions
        assert sessions.count(http) == sessions.count(plaza.http) == 1
        with pytest.raises(ClientClosedError):
            await c.workspaces.list()

    asyncio.run(run())


def test_reset_waits_without_blocking_the_borrower(client):
    from inspire.platform.web.flow import call
    from inspire.platform.web.transport_async import AsyncDriver

    async def run():
        transport = client._transport
        slot = transport._plaza_slot
        async with AsyncDriver(transport) as driver:
            await driver.execute(call(transport._borrow_plaza_slot, ("alpha", 1.0)))
            reset = asyncio.create_task(driver.execute(call(transport.reset_plaza_client)))
            try:
                await asyncio.sleep(0.02)
                assert not reset.done() and slot.busy
                transport._release_plaza_slot()
                await asyncio.wait_for(reset, 1)
                assert not slot.busy and slot.client is None
            finally:
                transport._release_plaza_slot()
                reset.cancel()
                await asyncio.gather(reset, return_exceptions=True)

    asyncio.run(run())
