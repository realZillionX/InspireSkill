"""Request policy and resumable authentication shared by both I/O drivers.

RequestCore yields decisions: observe a session generation, send, refresh or
wait. The blocking driver in inspire.platform.web.transport and the native
async driver in inspire.platform.web.transport_async return outcomes to the
same program, so retry and single-send rules cannot drift between them.
The core never infers write safety from an HTTP verb or an Action name.

The workflows below RequestCore describe acquisition and the 数据广场 handshake
with calls from inspire.platform.web.flow. They also contain local state and
persistence steps; the whole module is not a pure Sans-I/O state machine.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import time
from inspire.platform.web.flow import enter_context, exit_context

from inspire.platform.web.flow import Program as FlowProgram, workflow, call, http_call

from dataclasses import dataclass, field
from typing import Any, Generator, NoReturn

from inspire.platform.errors import (
    AuthenticationError,
    AuthenticationCooldownError,
    TransportError,
    ValidationError,
    SubmissionUncertainError,
    MutationUncertainError,
    WaitTimeoutError,
)


logger = logging.getLogger(__name__)


class _SingleSendViolation(RuntimeError):
    pass


def _classify_after_dispatch(error: Exception) -> Exception | None:
    """Return a definite rejection, or None when the write outcome is unknown."""
    from inspire.platform.web.session.models import TransientAPIError
    from inspire.platform.web.session.envelope import _THROTTLING_V2_ERROR_CODES

    from inspire.platform.web.plaza.core import PlazaRejected

    # Plaza business rejections carry raw envelope messages, without the
    # platform envelope's "API error:" prefix; HTTP status alone is insufficient.
    if isinstance(error, PlazaRejected):
        if error.status == 403:
            return AuthenticationError(str(error))
        if error.status is not None and error.status >= 500:
            return None
        return ValidationError(str(error))
    if isinstance(error, (AuthenticationError, TransportError)):
        return error
    # TransientAPIError is also a ValueError; classify it before business errors.
    if isinstance(error, TransientAPIError):
        # Messages may be redacted; use the envelope code, not its rendered text.
        code = (error.code or "").strip().lower().replace("_", "")
        if error.status == 429 or code in _THROTTLING_V2_ERROR_CODES:
            return TransportError(str(error))
        return None
    if isinstance(error, ValidationError) or (
        isinstance(error, ValueError) and str(error).startswith("API error:")
    ):
        return ValidationError(str(error))
    return None


@dataclass
class AuthenticationState:
    """Generation evidence shared immediately by all operation views."""

    force_browser: bool = False
    unproven_rebuild: float | None = None
    last_success: float | None = None


@dataclass
class SharedState:
    """Shared authentication evidence with an operation-local write claim."""

    authentication: AuthenticationState = field(default_factory=AuthenticationState)
    write: dict[str, Any] | None = None

    @property
    def force_browser(self) -> bool:
        return self.authentication.force_browser

    @force_browser.setter
    def force_browser(self, value: bool) -> None:
        self.authentication.force_browser = value

    @property
    def unproven_rebuild(self) -> float | None:
        return self.authentication.unproven_rebuild

    @unproven_rebuild.setter
    def unproven_rebuild(self, value: float | None) -> None:
        self.authentication.unproven_rebuild = value

    @property
    def last_success(self) -> float | None:
        return self.authentication.last_success

    @last_success.setter
    def last_success(self, value: float | None) -> None:
        self.authentication.last_success = value

    def dispatched(self) -> None:
        if self.write is not None:
            self.write["sent"] = True

    def refresh_guard(self, observed: float, can_refresh: bool) -> None:
        from inspire.platform.web.session.models import SessionExpiredError

        if self.unproven_rebuild is not None and observed >= self.unproven_rebuild:
            raise SessionExpiredError(
                "The session the last rebuild produced was refused as well. Not logging "
                "in again to replace a login nothing has been able to use."
            )
        if not can_refresh:
            raise SessionExpiredError("Session expired again after a single authentication refresh")

    def rebuilt(self, generation: float) -> None:
        self.unproven_rebuild = generation
        self.force_browser = False

    def success(self, observed: float, now: float, cli_compat: bool) -> None:
        if self.unproven_rebuild is not None and observed >= self.unproven_rebuild:
            self.unproven_rebuild = None
        self.last_success = now


def claim_write(state: dict[str, Any] | None) -> None:
    if state is not None:
        if state["used"]:
            raise _SingleSendViolation("single_send allows exactly one request.")
        state["used"] = True


def uncertain(state: dict[str, Any], error: Exception) -> NoReturn:
    if state["create"]:
        raise SubmissionUncertainError(state["operation_id"], inspect=state.get("inspect", "jobs")) from error
    raise MutationUncertainError("Mutation may have succeeded; inspect state.") from error


def remaining(timeout: float, deadline: float | None, now: float) -> float:
    value = timeout if deadline is None else deadline - now
    if value <= 0:
        raise WaitTimeoutError("Operation deadline exceeded; this does not stop remote workloads.")
    return value


@dataclass(frozen=True)
class Observe:
    """Acquire the session and sample time and its current generation."""


@dataclass(frozen=True)
class Observation:
    now: float
    generation: float
    jitter: float


@dataclass(frozen=True)
class Send:
    method: str
    path: str
    body: Any
    timeout: float
    browser: bool
    referer: str | None


@dataclass(frozen=True)
class ApplicationRequest:
    """An absolute-host request using a borrowed application cookie jar."""

    connection: Any
    options: dict[str, Any]
    allow_not_found: bool = False


@dataclass(frozen=True)
class Refresh:
    observed_generation: float
    can_refresh: bool


@dataclass(frozen=True)
class Sleep:
    delay: float


@dataclass(frozen=True)
class Return:
    payload: Any
    observed_generation: float


@dataclass(frozen=True)
class Raise:
    error: Exception


Action = Observe | Send | Refresh | Sleep | Return | Raise
Program = Generator[Action, Any, None]


class RequestCore:
    """Decide retries from driver outcomes, with one write claim per scope.

    Drivers must mark dispatch before sending: a failure before dispatch can be
    reported directly, while an unclassified failure after it is uncertain.
    CLI refresh/browser fallback and SDK attempt budgets intentionally differ;
    they are branches of this program, not separate request implementations.
    """

    def __init__(
        self,
        shared: SharedState,
        *,
        cli_compat: bool,
        allow_browser: bool,
        timeout: float,
        deadline: float | None,
    ) -> None:
        self.shared = shared
        self.cli_compat = cli_compat
        self.allow_browser = allow_browser
        self.timeout = timeout
        self.deadline = deadline

    def run(
        self,
        method: str,
        path: str,
        body: Any,
        timeout: float,
        referer: str | None,
    ) -> Program:
        try:
            yield from self._run(method, path, body, timeout, referer)
        except Exception as error:
            yield Raise(error)

    def _run(
        self,
        method: str,
        path: str,
        body: Any,
        timeout: float,
        referer: str | None,
    ) -> Program:
        from inspire.platform.web.transport_policy import is_request_error
        from inspire.platform.web.session.models import SessionExpiredError, TransientAPIError
        from inspire.platform.web.session.envelope import _is_transient_v2_error_code
        from inspire.platform.web.session.retry import backoff_delay

        state = self.shared.write
        claim_write(state)
        browser = self.shared.force_browser if self.cli_compat else False
        refreshed = False
        attempt = 0
        while attempt < (3 if state is None else 1):
            observation: Observation = yield Observe()
            request_timeout = (
                timeout
                if self.cli_compat and self.deadline is None
                else min(
                    timeout, self.timeout, remaining(self.timeout, self.deadline, observation.now)
                )
            )
            observed = observation.generation
            try:
                payload = yield Send(method, path, body, request_timeout, browser, referer)
                if not self.cli_compat and state is None and isinstance(payload, dict):
                    metadata = payload.get("ResponseMetadata")
                    envelope_error = metadata.get("Error") if isinstance(metadata, dict) else None
                    if isinstance(envelope_error, dict) and _is_transient_v2_error_code(
                        str(envelope_error.get("Code") or "")
                    ):
                        raise TransientAPIError(
                            str(envelope_error.get("Message") or envelope_error.get("Code")),
                            code=str(envelope_error.get("Code") or ""),
                        )
                yield Return(payload, observed)
                return
            except Exception as error:
                if state is not None:
                    if state["sent"]:
                        classified = _classify_after_dispatch(error)
                        if classified is error:
                            raise
                        if classified is not None:
                            raise classified from error
                        uncertain(state, error)
                    raise
                if self.cli_compat:
                    if isinstance(error, SessionExpiredError):
                        logger.debug("Web session expired; trying session renewal", exc_info=True)
                        yield Refresh(observed, not refreshed)
                        refreshed = True
                        browser = False
                        continue
                    # Body decoding is distinguished from local configuration errors.
                    from inspire.platform.web.transport_policy import _NonJSONResponse

                    if (
                        not browser
                        and (is_request_error(error) or isinstance(error, _NonJSONResponse))
                        and self.allow_browser
                    ):
                        logger.debug("HTTP request failed; trying browser fallback", exc_info=True)
                        self.shared.force_browser = browser = True
                        continue
                    if not isinstance(error, TransientAPIError) or attempt == 2:
                        raise
                    yield Sleep(backoff_delay(attempt, error, jitter=observation.jitter))
                    attempt += 1
                    continue
                if isinstance(error, SessionExpiredError):
                    if refreshed:
                        raise AuthenticationError(str(error)) from error
                    logger.debug("Web session expired; trying session renewal", exc_info=True)
                    yield Refresh(observed, True)
                    refreshed = True
                elif isinstance(error, (ValidationError, AuthenticationError)):
                    raise
                elif (is_request_error(error) or isinstance(error, ValueError)) and not isinstance(
                    error, TransientAPIError
                ):
                    if self.allow_browser:
                        logger.debug("HTTP request failed; trying browser fallback", exc_info=True)
                        browser = True
                elif not isinstance(error, TransientAPIError):
                    raise TransportError(str(error)) from error
                if attempt == 2:
                    raise TransportError(str(error)) from error
                clock: Observation = yield Observe()
                yield Sleep(
                    min(0.1 * (2**attempt), remaining(self.timeout, self.deadline, clock.now))
                )
                attempt += 1
        raise AssertionError("unreachable")




@workflow
def refresh(self: Any, *, require_cas_ticket: bool = False) -> FlowProgram[Any]:
    with self.scope(timeout=None):
        from inspire.platform.web.session.models import WebSession
        from inspire.platform.web.session.refresh_lock import exclusive_session_refresh
        from inspire.platform.web.session import auth

        previous = self._session.created_at if self._session else None
        if previous is not None:
            self._decisions.refresh_guard(previous, True)
        try:
            _context = exclusive_session_refresh(self.account, timeout=self.remaining())
            yield call(enter_context, _context)
            try:
                try:
                    cached = WebSession.load(allow_expired=True, account=self.account)
                    if not require_cas_ticket and cached and (previous is None or cached.created_at > previous):
                        self._adopt_session(cached)
                        return
                except Exception as error:
                    self.check_deadline()
                    if getattr(error, "retry_at", None) is not None:
                        raise
                    logger.debug("Cached session load failed; trying session renewal", exc_info=True)
                if self._session is not None and not require_cas_ticket:
                    renewed = (yield call(auth.renew_web_session_without_credentials, self._session))
                    if renewed is not None:
                        self._validate_session(renewed)
                        auth._persist(renewed, account=self.account)
                        self._adopt_session(renewed)
                        return
                logger.debug("No reusable cached session or SSO renewal; trying credential login")
                try:
                    username, password = auth.get_credentials(self.account)
                    self._adopt_session(
                        (yield call(auth.login_without_browser,
                            username, password, base_url=self.base_url, account=self.account
                        ))
                    )
                    self._decisions.rebuilt(self.session.created_at)
                except Exception as error:
                    self.check_deadline()
                    if (
                        not self.allow_browser
                        or getattr(error, "retry_at", None) is not None
                        or isinstance(error, auth.AuthenticationError)
                        or isinstance(error.__cause__, auth._CasVerificationRequired)
                    ):
                        raise
                    logger.debug("Credential login unavailable; trying browser login", exc_info=True)
                    self._adopt_session(
                        (yield call(auth.get_web_session, force_refresh=True, account=self.account))
                    )
                    self._decisions.rebuilt(self.session.created_at)
            finally:
                yield call(exit_context, _context, *sys.exc_info())
        except (AuthenticationError, WaitTimeoutError):
            raise
        except Exception as error:
            self.check_deadline()
            retry_at = getattr(error, "retry_at", None)
            if isinstance(retry_at, (int, float)):
                raise AuthenticationCooldownError(retry_at, str(error)) from error
            raise AuthenticationError(str(error)) from error
        finally:
            if self._browser is not None:
                # Browser cleanup must not replace the renewal result or error.
                with contextlib.suppress(Exception):
                    self._browser.close()
                self._browser = None


@workflow
def refresh_cli(self: Any, observed_created_at: float, *, can_refresh: bool) -> FlowProgram[None]:
    from inspire.platform.web import session as web_session

    # Capture the sent generation, not the current fields of a shared object.
    web_session.close_browser_client()
    with self._generation_lock:
        self._decisions.refresh_guard(observed_created_at, can_refresh)
        web_session.logger.debug("Web session expired; rebuilding it once for this call.")
        refreshed = (yield call(self._refresh_expired_session, observed_created_at))
        web_session.refresh_session_in_place(self.session, refreshed)
        self._decisions.rebuilt(self.session.created_at)


@workflow
def refresh_expired_session(self: Any, observed_created_at: float) -> FlowProgram[Any]:
    with self.scope(timeout=None):
        from inspire.platform.web import session as web_session

        session = self.session
        if session.created_at > observed_created_at:
            return session
        _context = web_session.exclusive_session_refresh(session.account)
        yield call(enter_context, _context)
        try:
            if session.created_at > observed_created_at:
                return session
            cached = web_session.WebSession.load(allow_expired=True, account=session.account)
            if (
                cached is not None
                and cached.storage_state.get("cookies")
                and cached.created_at > observed_created_at
            ):
                return cached
            renewed = (yield call(web_session.renew_web_session_without_credentials, session))
            if renewed is not None:
                web_session.logger.debug(
                    "Web session renewed through cached SSO state without credentials."
                )
                return renewed
            logger.debug("Cached SSO renewal unavailable; trying credential login")
            if not self.allow_browser:
                from inspire.platform.web.session import auth

                username, password = auth.get_credentials(session.account)
                return (yield call(auth.login_without_browser, username, password,
                                   base_url=self.base_url, account=session.account))
            return (yield call(web_session.acquire_web_session, force_refresh=True, account=session.account))
        finally:
            yield call(exit_context, _context, *sys.exc_info())


@workflow
def plaza_request(
    self: Any, method: str, path: str, *, params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None, timeout: float = 30
) -> FlowProgram[Any]:
    """Dispatch plaza calls with caller-owned authentication and retry budgets."""
    import requests
    from inspire.platform.web.plaza.core import (
        PLAZA_BASE_URL, PlazaError, PlazaNotSignedIn, CasTicketExpired, unwrap,
    )
    from inspire.platform.web.session.models import SessionExpiredError, TransientAPIError
    from inspire.platform.web.session.retry import backoff_delay

    self.check()
    state = self._write
    claim_write(state)
    auth_attempt, transient_attempt = 0, 0
    while True:
        self.check_deadline()
        session = self.session
        try:
            client = yield call(self._acquire_plaza_client, session, timeout)
            try:
                self._decisions.dispatched()
                response = yield http_call(
                    client.http.request, method.upper(), PLAZA_BASE_URL + path,
                    params=params, json=body, timeout=min(timeout, self.remaining()),
                    allow_redirects=False,
                )
                self.check_deadline()
                result = unwrap(response)
                return (yield call(self._finish, Return(result, session.created_at)))
            finally:
                self._release_plaza_slot()
        except Exception as error:
            if state is not None and state["sent"]:
                classified = _classify_after_dispatch(error)
                if classified is error:
                    raise
                if classified is not None:
                    raise classified from error
                self._uncertain(state, error)
            self.check_deadline()
            if isinstance(error, (SessionExpiredError, PlazaNotSignedIn)):
                yield call(self.reset_plaza_client)
                if auth_attempt == 2:
                    raise SessionExpiredError(
                        "The platform single-sign-on ticket could not be renewed; CAS still "
                        "cannot authenticate the data plaza." if isinstance(error, CasTicketExpired)
                        else "The data plaza rejected the refreshed platform session."
                    ) from error
                auth_attempt += 1
                if auth_attempt == 2:
                    try:
                        if isinstance(error, CasTicketExpired):
                            yield call(self._refresh, require_cas_ticket=True)
                        else:
                            yield call(self._refresh)
                    except AuthenticationError as refresh_error:
                        if self.cli_compat:
                            raise SessionExpiredError(str(refresh_error)) from refresh_error
                        raise
                continue
            if isinstance(error, TransientAPIError):
                if transient_attempt == 2 or state is not None:
                    raise
                yield call(time.sleep, min(backoff_delay(transient_attempt, error), self.remaining()))
                transient_attempt += 1
                continue
            if isinstance(error, requests.RequestException):
                raise PlazaError("The data plaza did not answer.") from error
            raise



@workflow
def acquire_session(self: Any) -> FlowProgram[None]:
    from inspire.platform.web import session as web_session

    with self.scope(timeout=None):
        if self.cli_compat:
            self._session = yield call(web_session.get_web_session, account=self.account)
            return
        cached = yield call(web_session.WebSession.load, allow_expired=True, account=self.account)
        try:
            self._session = self._validate_session(cached)
        except AuthenticationError:
            if not self.allow_browser:
                raise
            yield call(self._refresh)
