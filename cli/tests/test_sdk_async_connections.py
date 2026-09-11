"""Async resource lifetime contracts with isolated backends."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from inspire.sdk import InspireAsyncClient
from inspire.sdk.resources import Workspaces
from test_sdk import client as client
from test_sdk_async import tracked as tracked

pytestmark = pytest.mark.timeout(30, method="thread")


def test_one_operation_reuses_one_httpx_client(client, tracked, monkeypatch):
    built, closed = [], []
    real_init = httpx.AsyncClient.__init__
    real_close = httpx.AsyncClient.aclose

    def counting_init(self, *args, **kwargs):
        built.append(self)
        return real_init(self, *args, **kwargs)

    async def counting_close(self):
        closed.append(self)
        return await real_close(self)

    async def send(self, request, **kwargs):
        return httpx.Response(200, json={"ok": True}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", counting_init)
    monkeypatch.setattr(httpx.AsyncClient, "aclose", counting_close)
    monkeypatch.setattr(httpx.AsyncClient, "send", send)

    def rows(self):
        for _ in range(5):
            assert self.client._transport.request("GET", "/fake")["ok"]
        return []

    monkeypatch.setattr(Workspaces, "_all", rows)

    async def run():
        async with InspireAsyncClient("alpha") as c:
            await c.workspaces.list()
            first = len(built)
            assert not closed
            await c.workspaces.list()
            total = len(built)
            assert not closed
        await c.close()
        return first, total

    first, total = asyncio.run(run())
    assert first == 1, f"one operation with 5 requests built {first} httpx clients"
    assert total == 1, f"two operations built {total} httpx clients"

    assert closed == built
