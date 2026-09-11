"""Caller-owned web state and the blocking driver for shared request decisions.

Console requests, application requests and 数据广场 use this owner for account,
session generation, deadlines and write classification. Request policy lives in
inspire.platform.web.transport_core; native async I/O interprets it through
inspire.platform.web.transport_async. CLI compatibility selects response and
renewal policy within that design, not another dispatcher.

SDK writes explicitly enter single_send. A preflight read may refresh the
session; once the write is dispatched, no driver may replay it.
"""

from __future__ import annotations

import contextlib

from inspire.platform.web.flow import Program as FlowProgram, workflow, call, http_call
import os
import threading
import time
from contextlib import contextmanager, nullcontext, asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Iterator, NoReturn

from inspire.platform.errors import (
    AuthenticationError,
    AuthenticationCooldownError,  # noqa: F401 - SDK compatibility export
    WaitTimeoutError,  # noqa: F401 - SDK compatibility export
    ClientClosedError,
    ClientThreadError,
    TransportError,
    ValidationError,
    SubmissionUncertainError,
    MutationUncertainError,
)


if TYPE_CHECKING:
    from inspire.platform.web.plaza.core import PlazaClient
    from inspire.platform.web.session.models import WebSession

from inspire.platform.web.transport_policy import (  # noqa: F401 - compatibility imports
    _NonJSONResponse, _SDK_POLICY, _CLI_POLICY, http_options,
)
from inspire.platform.web.transport_core import (
    ApplicationRequest, RequestCore, SharedState, Observe, Observation, Send, Refresh, Sleep, Return, Raise, Action,
    _SingleSendViolation, _classify_after_dispatch, remaining, uncertain,
)

class PlazaSlot:
    """A sign-in and its borrow state shared by operation views."""

    def __init__(self) -> None:
        self.lock = threading.Condition()
        self.busy = False
        self.client: PlazaClient | None = None
        self.key: tuple[str | None, float] | None = None


class Transport:
    """Own dispatch and authentication state for a caller.

    ``cli_compat`` preserves the CLI's HTTP messages, Retry-After waits, shared
    thread-local pools and renewal policy. The SDK defaults and single-send
    contract remain independent of that presentation/compatibility policy.
    """

    def __init__(
        self,
        account: str | None,
        base_url: str,
        *,
        username: str,
        allow_browser: bool = False,
        timeout: float = 30,
        cli_compat: bool = False,
    ):
        self.account, self.base_url = account, base_url.rstrip("/")
        self.username, self.allow_browser, self.timeout = username, allow_browser, timeout
        self._pid, self._thread = os.getpid(), threading.get_ident()
        self.cli_compat = cli_compat
        self._decisions = SharedState()
        self._generation_lock = threading.RLock()
        self._plaza_slot = PlazaSlot()
        self._closed = False
        self._session: Any = None
        self._http: Any = None
        self._browser: Any = None
        self.deadline: float | None = None
        self._tls_contexts: dict[Any, Any] = {}
        self._preparation_lock = threading.Lock()
        from inspire.platform.web.session.requests import CachedNetrcAuth

        self._netrc_auth = CachedNetrcAuth()

    @property
    def _force_browser(self) -> bool:
        return self._decisions.force_browser

    @_force_browser.setter
    def _force_browser(self, value: bool) -> None:
        self._decisions.force_browser = value

    @property
    def _unproven_rebuild(self) -> float | None:
        return self._decisions.unproven_rebuild

    @_unproven_rebuild.setter
    def _unproven_rebuild(self, value: float | None) -> None:
        self._decisions.unproven_rebuild = value

    @property
    def _last_success(self) -> float | None:
        return self._decisions.last_success

    @_last_success.setter
    def _last_success(self, value: float | None) -> None:
        self._decisions.last_success = value

    @property
    def _write(self) -> dict[str, Any] | None:
        return self._decisions.write

    @_write.setter
    def _write(self, value: dict[str, Any] | None) -> None:
        self._decisions.write = value

    def check(self) -> None:
        if self._pid != os.getpid() or (
            not self.cli_compat and self._thread != threading.get_ident()
        ):
            raise ClientThreadError("Create and use each Client in the same process and thread.")
        if self._closed:
            raise ClientClosedError("Client is closed.")

    def remaining(self) -> float:
        self.check()
        return remaining(self.timeout, self.deadline, time.monotonic())

    def check_deadline(self) -> None:
        """Raise WaitTimeoutError when the operation budget is exhausted."""
        self.remaining()

    @contextmanager
    def scope(self, *, timeout: float | None = 120) -> Iterator[None]:
        """Bind this transport; None adds no deadline and preserves any outer one."""
        from inspire.accounts import account_scope
        from inspire.platform.web.runtime import active_transport

        self.check()
        old = self.deadline
        if timeout is not None:
            self.deadline = min(old, time.monotonic() + timeout) if old else time.monotonic() + timeout
        token = active_transport.set(self)
        try:
            with nullcontext() if self.cli_compat else account_scope(self.account):
                yield
        finally:
            active_transport.reset(token)
            self.deadline = old

    @contextmanager
    def single_send(self, operation_id: str = "", *, create: bool = False, inspect: str = "jobs") -> Iterator[None]:
        """Allow at most one request, reporting an unknown dispatched outcome.

        A recent returned console, application or plaza response skips the
        preflight read. This timestamp does not prove business success.
        operation_id labels an uncertain creation; it is not an idempotency key.
        """
        self.check()
        if self._write is not None:
            raise _SingleSendViolation("single_send blocks cannot be nested.")
        if self._last_success is None or time.monotonic() - self._last_success >= 60:
            from inspire.platform.web.session.auth import USER_DETAIL_PATH
            from inspire.platform.web.session.envelope import _v2_result

            _v2_result(self.request("POST", USER_DETAIL_PATH, body={}))
        # The probe is a READ. A dispatched write must never be replayed;
        # only failures without a definite rejection remain uncertain.
        state = {"operation_id": operation_id, "create": create, "inspect": inspect, "used": False, "sent": False}
        self._write = state
        try:
            yield
        except (
            SubmissionUncertainError,
            MutationUncertainError,
            _SingleSendViolation,
            ValidationError,
            AuthenticationError,
    AuthenticationCooldownError,
    WaitTimeoutError,
            TransportError,
        ):
            raise
        except Exception as error:
            if state["sent"]:
                classified = _classify_after_dispatch(error)
                if classified is not None:
                    raise classified from error
                self._uncertain(state, error)
            raise
        finally:
            self._write = None

    def _uncertain(self, state: dict[str, Any], error: Exception) -> NoReturn:
        uncertain(state, error)

    def _validate_session(self, session):
        if (
            session is None
            or not session.storage_state.get("cookies")
            or (session.base_url or "").rstrip("/") != self.base_url
            or session.account not in (None, self.account)
        ):
            raise AuthenticationError("No matching cached session; initialize this account first.")
        return session

    @property
    def session(self):
        self.check()
        if self._session is None:
            self._acquire_session()
        return self._session

    @workflow
    def _acquire_session(self) -> FlowProgram[None]:
        from inspire.platform.web.transport_core import acquire_session

        yield call(acquire_session, self)

    def adopt_session(self, session: WebSession) -> None:
        """Use an already acquired session without consulting disk or logging in."""
        self.check()
        if self.cli_compat:
            self._session = session
        else:
            self._adopt_session(session)

    def _adopt_session(self, session):
        from inspire.platform.web.session.requests import _configure

        self._session = self._validate_session(session)
        if self._http is not None:
            _configure(self._http, self._session, self.base_url)

    @workflow
    def _refresh_expired_session(self, observed_created_at: float) -> FlowProgram[Any]:
        from inspire.platform.web.transport_core import refresh_expired_session

        return (yield call(refresh_expired_session, self, observed_created_at))

    @workflow
    def _refresh_cli(self, observed_created_at: float, *, can_refresh: bool) -> FlowProgram[None]:
        from inspire.platform.web.transport_core import refresh_cli

        return (yield call(refresh_cli, self, observed_created_at, can_refresh=can_refresh))

    @workflow
    def _refresh(self, *, require_cas_ticket: bool = False) -> FlowProgram[Any]:
        from inspire.platform.web.transport_core import refresh

        return (yield call(refresh, self, require_cas_ticket=require_cas_ticket))

    def _once(
        self, method, path, body, timeout, browser=False, referer=None, *, policy=None,
        _disposable=False,
    ):
        from inspire.platform.web import session as web_session

        if isinstance(body, ApplicationRequest):
            return self._application_send(method, path, body, timeout)
        policy = policy or (_CLI_POLICY if self.cli_compat else _SDK_POLICY)
        url = path if path.startswith(("https://", "http://")) else self.base_url + path
        headers = {"Referer": referer} if referer else {}
        if not browser:
            args: tuple[Any, ...]
            if self.cli_compat:
                http = web_session.pooled_requests_session(self.session, url)
            else:
                from inspire.platform.web.session.requests import build_requests_session, _configure

                if self._http is None:
                    self._http = build_requests_session(self.session, url)
                else:
                    _configure(self._http, self.session, url)
                http = self._http
            options = http_options(method, body, referer, self.base_url, timeout, self.cli_compat)
            kwargs: dict[str, Any] = {
                "headers": options.headers, "timeout": timeout, "allow_redirects": False,
            }
            if options.include_json:
                kwargs["json"] = options.body
            if self.cli_compat:
                http_send, args = getattr(http, options.method.lower()), (url,)
            else:
                kwargs["timeout"] = (options.connect_timeout, timeout)
                http_send, args = http.request, (options.method, url)
            return policy.response(self._dispatch(http_send, *args, **kwargs))

        from inspire.platform.web.browser_api.core import _in_asyncio_loop, _run_in_thread

        disposable = _disposable or (self.cli_compat and _in_asyncio_loop())

        def send():
            if self.cli_compat:
                client = (
                    web_session.create_browser_client(self.session) if disposable
                    else web_session.get_browser_client(self.session)
                )
            elif disposable:
                client = web_session.create_browser_client(self.session)
            else:
                if self._browser is None:
                    self._browser = web_session.create_browser_client(self.session)
                client = self._browser
            try:
                # The SDK historically sends no browser Referer override.
                kwargs = {"headers": headers} if self.cli_compat else {}
                return self._dispatch(
                    client.request_json, method, url, body=body, timeout=timeout, **kwargs
                )
            finally:
                if disposable:
                    client.close()

        try:
            return _run_in_thread(send) if disposable and not _disposable else send()
        except Exception as error:
            policy.browser_error(error)

    @contextmanager
    def application_connection(self, url: str) -> Iterator[Any]:
        """Borrow a private application jar; every send still uses this dispatcher."""
        from inspire.platform.web.application import ApplicationConnection
        from inspire.platform.web.session.requests import build_requests_session

        self.check()
        http = build_requests_session(self.session, url)
        connection = ApplicationConnection(self, http, self.session.created_at)
        try:
            yield connection
        finally:
            http.close()

    @asynccontextmanager
    async def application_connection_async(self, url: str) -> AsyncIterator[Any]:
        """Drive the same application jar lifecycle from native async callers."""
        from inspire.platform.web.transport_async import AsyncDriver
        from inspire.platform.web.flow import run_sync
        import sys

        context = self.application_connection(url)
        async with AsyncDriver(self) as driver:
            connection = await run_sync(driver, context.__enter__)
            try:
                yield connection
            finally:
                error = sys.exc_info()
                await run_sync(driver, lambda: context.__exit__(*error))

    @workflow
    def _application_send(
        self, method: str, url: str, body: ApplicationRequest, timeout: float,
    ) -> FlowProgram[Any]:
        from inspire.platform.web.transport_policy import classify_application_response

        connection = body.connection
        if connection.generation != self.session.created_at:
            connection.reconfigure(self.session, url)
        options = dict(body.options)
        options["timeout"] = (min(5, timeout), timeout)
        self._decisions.dispatched()
        response = yield http_call(getattr(connection.http, method.lower()), url, **options)
        self.check_deadline()
        if not (body.allow_not_found and method.upper() == "GET" and response.status_code == 404):
            classify_application_response(response)
        return response

    def _dispatch(self, send: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        self._decisions.dispatched()
        return send(*args, **kwargs)

    def _core(self) -> RequestCore:
        return RequestCore(
            self._decisions, cli_compat=self.cli_compat, allow_browser=self.allow_browser,
            timeout=self.timeout, deadline=self.deadline,
        )

    def _observe(self) -> Observation:
        import random

        self.check()
        generation = self.session.created_at
        return Observation(time.monotonic(), generation, random.random())

    def _finish(self, action: Return) -> Any:
        with self._generation_lock:
            self._decisions.success(action.observed_generation, time.monotonic(), self.cli_compat)
        return action.payload

    def _perform(self, action: Action) -> Any:
        if isinstance(action, Observe):
            return self._observe()
        if isinstance(action, Send):
            return self._once(
                action.method, action.path, action.body, action.timeout, action.browser, action.referer
            )
        if isinstance(action, Refresh):
            if self.cli_compat:
                return self._refresh_cli(
                    action.observed_generation, can_refresh=action.can_refresh
                )
            return self._refresh()
        if isinstance(action, Sleep):
            time.sleep(action.delay)
            return None
        raise AssertionError(action)

    def request(
        self, method: str, path: str, *, body=None, timeout: float = 30, referer=None
    ) -> dict:
        from inspire.platform.web.flow import async_call

        bridge = async_call.get()
        if bridge is not None:
            return bridge(call(self.request_async, method, path, body=body, timeout=timeout, referer=referer))
        program = self._core().run(method, path, body, timeout, referer)
        try:
            action = next(program)
            while True:
                if isinstance(action, Return):
                    return self._finish(action)
                if isinstance(action, Raise):
                    raise action.error
                try:
                    result = self._perform(action)
                except Exception as error:
                    action = program.throw(error)
                else:
                    action = program.send(result)
        finally:
            program.close()

    async def request_async(
        self, method: str, path: str, *, body=None, timeout: float = 30, referer=None
    ) -> dict:
        """Native JSON path used by InspireAsyncClient."""
        from inspire.platform.web.transport_async import AsyncDriver

        self.check()
        program = self._core().run(method, path, body, timeout, referer)
        async with AsyncDriver(self) as driver:
            try:
                action = next(program)
                while True:
                    if isinstance(action, Return):
                        return self._finish(action)
                    if isinstance(action, Raise):
                        raise action.error
                    try:
                        result = await driver.perform(action)
                    except Exception as error:
                        action = program.throw(error)
                    else:
                        action = program.send(result)
            finally:
                program.close()

    @workflow
    def reset_plaza_client(self) -> FlowProgram[None]:
        """Discard this owner's sign-in once its current borrower finishes."""
        self.check()
        yield call(self._borrow_plaza_slot, None)
        self._release_plaza_slot()

    def _try_borrow_plaza_slot(self, key: tuple[str | None, float] | None) -> tuple[bool, Any]:
        slot = self._plaza_slot
        with slot.lock:
            if slot.busy:
                return False, None
            if key is None or slot.key != key:
                if slot.client is not None:
                    slot.client.close()
                slot.client, slot.key = None, None
            slot.busy = True
            return True, slot.client

    @workflow
    def _borrow_plaza_slot(self, key: tuple[str | None, float] | None) -> FlowProgram[Any]:
        while True:
            self.check_deadline()
            acquired, client = yield call(self._try_borrow_plaza_slot, key)
            if acquired:
                return client
            yield call(time.sleep, 0.01)

    def _release_plaza_slot(self) -> None:
        with self._plaza_slot.lock:
            self._plaza_slot.busy = False
            self._plaza_slot.lock.notify_all()

    @workflow
    def _acquire_plaza_client(self, session: WebSession, timeout: float) -> FlowProgram[Any]:
        from inspire.platform.web.plaza.core import sign_in

        key = (session.account, session.created_at)
        client = yield call(self._borrow_plaza_slot, key)
        try:
            if client is None:
                client = yield call(sign_in, session, self, timeout)
                with self._plaza_slot.lock:
                    self._plaza_slot.client, self._plaza_slot.key = client, key
            return client
        except BaseException:
            self._release_plaza_slot()
            raise

    @contextmanager
    def _plaza_client(self, session: WebSession, timeout: float) -> Iterator[PlazaClient]:
        client = self._acquire_plaza_client(session, timeout)
        try:
            yield client
        finally:
            self._release_plaza_slot()

    @workflow
    def plaza_request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None, timeout: float = 30,
    ) -> FlowProgram[Any]:
        from inspire.platform.web.transport_core import plaza_request

        return (yield call(plaza_request, self, method, path,
                           params=params, body=body, timeout=timeout))

    async def plaza_request_async(
        self, method: str, path: str, *, params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None, timeout: float = 30,
    ) -> Any:
        from inspire.platform.web.transport_async import AsyncDriver

        async with AsyncDriver(self) as driver:
            return await driver.execute(call(
                self.plaza_request, method, path, params=params, body=body, timeout=timeout,
            ))

    def release_view(self, owner: Transport) -> None:
        """Retire an operation without tearing down resources borrowed from its owner."""
        for name in ("_http", "_browser"):
            resource = getattr(self, name)
            inherited = getattr(owner, name)
            if inherited is None:
                setattr(owner, name, resource)
            elif resource is not None and resource is not inherited:
                with contextlib.suppress(Exception):
                    resource.close()
        self._session = self._browser = self._http = None
        self._closed = True

    def close(self) -> None:
        if self._closed:
            return
        self.check()
        # Closing one stale resource must not prevent releasing the remaining resources.
        with contextlib.suppress(Exception):
            self.reset_plaza_client()
        if self.cli_compat:
            from inspire.platform.web import session as web_session

            with contextlib.suppress(Exception):
                web_session.close_browser_client()
            with contextlib.suppress(Exception):
                web_session.close_pooled_requests_session()
        for resource in (self._browser, self._http):
            if resource is not None:
                with contextlib.suppress(Exception):
                    resource.close()
        self._session = self._browser = self._http = None
        self._closed = True
