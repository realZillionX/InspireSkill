"""Native HTTP contracts against loopback sockets, without a platform connection."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import socket

import httpx
import pytest

from inspire.sdk import InspireAsyncClient
from inspire.sdk.resources import Workspaces
from test_sdk import client as client
from test_sdk_async import tracked as tracked

pytestmark = pytest.mark.timeout(30, method="thread")
REAL_SEND = httpx.AsyncClient.send


@asynccontextmanager
async def local_http(handler):
    tasks = set()
    connections = []

    async def serve(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        connections.append(writer)
        try:
            while True:
                data = await reader.readuntil(b"\r\n\r\n")
                path = data.split(b" ")[1].decode()
                await handler(path)
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n{"ok":true}')
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1], connections
    finally:
        server.close()
        await server.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def wire_requests(monkeypatch, port, path, count=1):
    monkeypatch.setattr(httpx.AsyncClient, "send", REAL_SEND)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)

    def rows(self):
        for _ in range(count):
            assert self.client._transport.request("GET", f"http://localhost:{port}/{path()}")["ok"]
        return []

    monkeypatch.setattr(Workspaces, "_all", rows)


def test_keepalive_resolves_once_across_operations(client, tracked, monkeypatch):
    lookups = []
    resolve = socket.getaddrinfo

    def counted(host, port, *args, **kwargs):
        assert host in (b"localhost", "localhost")
        lookups.append(host)
        return resolve("127.0.0.1", port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", counted)

    async def handler(path):
        pass

    async def run():
        async with local_http(handler) as (port, connections):
            wire_requests(monkeypatch, port, lambda: "fast", count=5)
            async with InspireAsyncClient("alpha") as c:
                await c.workspaces.list()
                await c.workspaces.list()
            assert len(connections) == 1
        assert len(lookups) == 1

    asyncio.run(run())


@pytest.mark.parametrize("concurrency", [None, 1, 2])
def test_requests_overlap_on_wire(client, tracked, monkeypatch, concurrency):
    async def run():
        arrived = asyncio.Event()
        release = asyncio.Event()
        active = maximum = 0

        async def handler(path):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            if active == 4:
                arrived.set()
            await release.wait()
            active -= 1

        async with local_http(handler) as (port, connections):
            wire_requests(monkeypatch, port, lambda: "overlap")
            if concurrency is None:
                c = InspireAsyncClient("alpha")
            else:
                with pytest.warns(DeprecationWarning):
                    c = InspireAsyncClient("alpha", concurrency=concurrency)
            async with c:
                tasks = [asyncio.create_task(c.workspaces.list()) for _ in range(4)]
                try:
                    await asyncio.wait_for(arrived.wait(), 2)
                    assert len(connections) == maximum == 4
                    assert all(not task.done() for task in tasks)
                finally:
                    release.set()
                    await asyncio.gather(*tasks)

    asyncio.run(run())


def test_slow_response_leaves_other_request_runnable(client, tracked, monkeypatch):
    async def run():
        slow = asyncio.Event()
        release = asyncio.Event()

        async def handler(path):
            if path == "/slow":
                slow.set()
                await release.wait()

        paths = iter(("slow", "fast"))
        async with local_http(handler) as (port, connections):
            wire_requests(monkeypatch, port, lambda: next(paths))
            async with InspireAsyncClient("alpha") as c:
                task = asyncio.create_task(c.workspaces.list())
                try:
                    await asyncio.wait_for(slow.wait(), 2)
                    await asyncio.wait_for(c.workspaces.list(), 2)
                    assert not task.done()
                    assert len(connections) == 2
                finally:
                    release.set()
                    await task

    asyncio.run(run())
