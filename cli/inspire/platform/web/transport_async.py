"""Native I/O driver for the same decisions the blocking Transport executes.

HTTP uses httpx; requests still prepares bodies, cookies, netrc and proxy
settings so changing execution mode does not change the wire contract.
Authentication workflows suspend on native HTTP, Playwright and lock waits.
SSH/SCP command builders keep their synchronous shape but run with asyncio
subprocesses; local file and certificate work uses inspire.platform.web.offload.

SDK connections belong to the async client and are reused across operations.
Standalone Transport.request_async calls keep driver-scoped connections, closed
on driver exit. HTTP DNS uses the loop default executor: sequential keep-alive
requests resolve once per host per client while the connection survives; new
connections (concurrency, expiry or reconnects) may resolve again. PTY connections
also use the loop default executor for DNS. No custom resolver cache is added.
"""

from __future__ import annotations

from inspire.platform.web.offload import offload

import asyncio
import inspect
import time
from contextlib import AsyncExitStack, suppress
from contextvars import ContextVar
from typing import Any, TYPE_CHECKING

import httpx
import requests

from inspire.platform.web.transport_core import ApplicationRequest, Observe, Send, Refresh, Sleep, Action
from inspire.platform.web.flow import Call, call, program_for, enter_context, exit_context, run_sync, drive_program
from inspire.platform.web.transport_policy import _CLI_POLICY, _SDK_POLICY, http_options
from inspire.platform.web.session.requests import build_requests_session

if TYPE_CHECKING:
    from inspire.platform.web.transport import Transport


class AsyncClientPool:
    """HTTP connections owned by one SDK runtime on its event loop."""

    def __init__(self) -> None:
        self.clients: dict[tuple[Any, ...], httpx.AsyncClient] = {}
        self.lock = asyncio.Lock()

    async def aclose(self) -> None:
        clients, self.clients = self.clients, {}
        for client in clients.values():
            with suppress(Exception):
                await client.aclose()


current_client_pool: ContextVar[AsyncClientPool | None] = ContextVar(
    "inspire_async_client_pool", default=None,
)


class AsyncDriver:
    """Supply I/O outcomes without owning a second retry or authentication policy.

    Adapted contexts and standalone HTTP clients close on driver exit. Under
    an SDK runtime, HTTP clients are borrowed from its ContextVar pool instead.
    Identity and session state belong to the Transport.
    """

    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self.stack = AsyncExitStack()
        self.clients: dict[tuple[Any, ...], httpx.AsyncClient] = {}
        self.contexts: dict[int, Any] = {}

    async def __aenter__(self) -> AsyncDriver:
        await self.stack.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        # The stack attempts every close; cleanup must not replace the request outcome.
        with suppress(Exception):
            await self.stack.__aexit__(*args)

    async def _browser_send(self, action: Send) -> Any:
        from inspire.platform.web.session.browser_client import AsyncBrowserRequestClient

        owner = self.transport
        owner.check()
        if isinstance(action.body, ApplicationRequest):
            return await self._send(action)
        policy = _CLI_POLICY if owner.cli_compat else _SDK_POLICY
        url = action.path if action.path.startswith(("https://", "http://")) else owner.base_url + action.path
        kwargs = {"headers": {"Referer": action.referer} if action.referer else {}} if owner.cli_compat else {}
        try:
            async with AsyncBrowserRequestClient(owner.session) as client:
                return await owner._dispatch(
                    client.request_json, action.method, url, body=action.body,
                    timeout=action.timeout, **kwargs,
                )
        except Exception as error:
            from inspire.platform.web import session as web_session

            if owner.cli_compat and web_session.is_playwright_browser_runtime_error(error):
                # The async disposable context already closed its own resources.
                web_session.raise_browser_runtime_error(error)
            policy.browser_error(error)

    async def perform(self, action: Action) -> Any:
        owner = self.transport
        if isinstance(action, Observe):
            if owner._session is None:
                await self.execute(call(owner._acquire_session))
            return owner._observe()
        if isinstance(action, Refresh):
            function = owner._refresh_cli if owner.cli_compat else owner._refresh
            args = (action.observed_generation,) if owner.cli_compat else ()
            kwargs = {"can_refresh": action.can_refresh} if owner.cli_compat else {}
            return await self.execute(call(function, *args, **kwargs))
        if isinstance(action, Sleep):
            await asyncio.sleep(action.delay)
            return None
        if isinstance(action, Send):
            if action.browser:
                return await self._browser_send(action)
            return await self._send(action)
        raise AssertionError(action)

    async def _send(self, action: Send) -> Any:
        owner = self.transport
        owner.check()
        if isinstance(action.body, ApplicationRequest):
            return await self.execute(call(
                owner._application_send, action.method, action.path, action.body, action.timeout,
            ))
        url = action.path if action.path.startswith(("https://", "http://")) else owner.base_url + action.path
        # requests only prepares bytes here; it never sends async API traffic.
        # This preserves JSON encoding, cookie domain/path matching, default
        # headers, netrc and explicit/system proxy precedence exactly.
        options = http_options(
            action.method, action.body, action.referer, owner.base_url,
            action.timeout, owner.cli_compat,
        )

        session = owner.session

        def prepare() -> Any:
            with build_requests_session(session, url) as preparation:
                return self._prepare(preparation, requests.Request(
                    options.method, url, headers=options.headers, json=options.body,
                ))

        prepared, settings = await offload(prepare)
        client = await self._client(prepared.url, settings)
        timeout = httpx.Timeout(action.timeout, connect=options.connect_timeout)
        request = httpx.Request(
            prepared.method or action.method, prepared.url or url,
            headers=dict(prepared.headers), content=prepared.body,
            extensions={"timeout": timeout.as_dict()},
        )
        response = await owner._dispatch(client.send, request, follow_redirects=False)
        policy = _CLI_POLICY if owner.cli_compat else _SDK_POLICY
        return policy.response(response)

    async def execute(self, action: Call) -> Any:
        """Interpret nested workflows without moving authentication into a worker."""
        from inspire.services.execution import remote_exec
        from inspire.platform.web.browser_api import jupyter_terminal

        from inspire.bridge.tunnel.ssh_exec import _stream_process
        from inspire.bridge.tunnel.process_async import stream_process, run_process
        import subprocess

        if action.function is subprocess.run:
            return await run_process(*action.args, **action.kwargs)
        if action.function is _stream_process:
            async def deliver(callback: Any, chunk: str) -> None:
                result = await run_sync(self, lambda: callback(chunk))
                if inspect.isawaitable(result):
                    await result
            return await stream_process(*action.args, **action.kwargs, deliver=deliver)

        if action.blocking:
            from inspire.services.execution.async_output import _finish_io
            from functools import partial

            return await _finish_io(partial(action.function, *action.args, **action.kwargs))

        from inspire.platform.web.session.auth import _playwright_context
        if action.function is _playwright_context:
            from playwright.async_api import async_playwright

            return async_playwright()

        adapters: dict[Any, Any] = {
            remote_exec.exec_over_pty_websocket: remote_exec.exec_over_pty_websocket_async,
            remote_exec.exec_in_notebook_jupyter: remote_exec.exec_in_notebook_jupyter_async,
            jupyter_terminal.run_command_capture_in_notebook: jupyter_terminal.run_command_capture_in_notebook_async,
        }
        if inspect.iscoroutinefunction(action.function):
            return await action.function(*action.args, **action.kwargs)
        adapter = adapters.get(action.function)
        if adapter is not None:
            return await adapter(*action.args, **action.kwargs)
        if action.http:
            return await self._http(action)
        if action.function is enter_context:
            from inspire.platform.web.async_context import authentication_context

            context = action.args[0]
            if hasattr(context, "__aenter__"):
                result = await context.__aenter__()
                self.contexts[id(context)] = context
                return result
            if not hasattr(context, "func"):
                return context.__enter__()
            asynchronous = authentication_context(context, self.transport)
            await asynchronous.__aenter__()
            self.contexts[id(context)] = asynchronous
            return None
        if action.function is exit_context:
            context, *error = action.args
            asynchronous = self.contexts.pop(id(context), None)
            if asynchronous is None:
                return context.__exit__(*error)
            return await asynchronous.__aexit__(*error)
        if action.function is time.sleep:
            await asyncio.sleep(*action.args)
            return None
        program = program_for(action)
        if program is not None:
            return await run_sync(self, lambda: drive_program(program))
        result = await run_sync(self, lambda: action.function(*action.args, **action.kwargs))
        return await result if inspect.isawaitable(result) else result

    async def _http(self, action: Call) -> requests.Response:
        """Send prepared requests with native redirects and a caller-owned cookie jar."""
        owner = self.transport
        http = getattr(action.function, "__self__")
        method = action.function.__name__
        args = action.args
        if method == "request":
            method, url, *rest = args
        else:
            url, *rest = args
        kwargs = dict(action.kwargs)
        follow = kwargs.pop("allow_redirects", True)
        budget = kwargs.pop("timeout", owner.timeout)
        if isinstance(budget, tuple):
            connect, budget = budget
        else:
            connect = budget
        budget = min(budget, owner.remaining())
        prepared, settings = await offload(
            self._prepare, http, requests.Request(method.upper(), url, **kwargs),
        )
        client = await self._client(prepared.url, settings)
        client.cookies.clear()
        client.cookies.update(http.cookies)
        request = httpx.Request(
            prepared.method, prepared.url, headers=dict(prepared.headers), content=prepared.body,
            extensions={"timeout": httpx.Timeout(budget, connect=min(connect, budget)).as_dict()},
        )
        try:
            response = await asyncio.wait_for(client.send(request, follow_redirects=follow), budget)
        except (httpx.RequestError, TimeoutError) as error:
            owner.check_deadline()
            if isinstance(error, (httpx.TimeoutException, TimeoutError)):
                raise requests.Timeout(str(error)) from error
            if isinstance(error, httpx.TooManyRedirects):
                raise requests.TooManyRedirects(str(error)) from error
            raise requests.ConnectionError(str(error)) from error
        owner.check_deadline()
        for cookie in client.cookies.jar:
            http.cookies.set_cookie(cookie)
        for item in [*response.history, response]:
            for cookie in item.cookies.jar:
                http.cookies.set_cookie(cookie)
        result = requests.Response()
        result.status_code = response.status_code
        result.headers.update(response.headers)
        result._content = response.content
        result.url = str(response.url) if response._request is not None else str(prepared.url)
        result.encoding = response.encoding
        result.request = prepared
        return result


    def _prepare(self, http: requests.Session, request: requests.Request) -> Any:
        # requests retains cookie/header/body and environment proxy semantics.
        # A truthy auth callable suppresses its repeated implicit netrc lookup.
        if http.trust_env and not http.auth and not request.auth:
            request.auth = self.transport._netrc_auth
        prepared = http.prepare_request(request)
        return prepared, http.merge_environment_settings(prepared.url, {}, None, None, None)

    async def _client(self, url: str, settings: Any) -> httpx.AsyncClient:
        pool = current_client_pool.get()
        if pool is not None:
            async with pool.lock:
                return await self._pooled_client(url, settings, pool.clients, shared=True)
        return await self._pooled_client(url, settings, self.clients, shared=False)

    async def _pooled_client(self, url: str, settings: Any,
                             clients: dict[tuple[Any, ...], httpx.AsyncClient],
                             *, shared: bool) -> httpx.AsyncClient:
        proxy = requests.utils.select_proxy(url, settings["proxies"])
        key = (proxy, settings["verify"], settings["cert"])
        client = clients.get(key)
        if client is None:
            def build() -> tuple[Any, Any]:
                from httpx import create_ssl_context

                tls_key = (settings["verify"], settings["cert"])
                contexts = self.transport._tls_contexts
                # The shared lock is held only in workers, never across awaits.
                with self.transport._preparation_lock:
                    if tls_key not in contexts:
                        contexts[tls_key] = create_ssl_context(
                            verify=settings["verify"], cert=settings["cert"], trust_env=False,
                        )
                    context = contexts[tls_key]
                    proxy_config: Any = proxy
                    if proxy and proxy.startswith("https://"):
                        if (True, None) not in contexts:
                            contexts[(True, None)] = create_ssl_context(trust_env=False)
                        proxy_config = httpx.Proxy(proxy, ssl_context=contexts[(True, None)])
                return proxy_config, context
            proxy_config, context = await offload(build)
            # Ownership starts after the cancellable file preparation has finished.
            client = httpx.AsyncClient(proxy=proxy_config, verify=context,
                                       trust_env=False, follow_redirects=False)
            if not shared:
                await self.stack.enter_async_context(client)
            clients[key] = client
        return client
