"""Session handshake and transport for 数据广场 on ``aip.sii.edu.cn``.

数据广场 (上海创智学院数据广场) is a different application from the qz console:
another host, another REST style, and its own session cookie. The qz console's
数据集 sidebar entry is only an external link to it, and it is the sole place the
dataset catalogue can be browsed or searched — qz's own ``/api/v2/dataset``
route carries a single ``ValidateDataset`` Action and no listing at all (see
:mod:`inspire.platform.web.browser_api.datasets`).

Signing in needs no browser. The account's separately stored CAS session holds the CAS
ticket-granting cookie, which is enough to mint a service ticket for the plaza:

1. ``GET {CAS}/cas/login?service=<plaza>/`` → 302 whose ``Location`` carries
   ``?ticket=ST-…``; the redirect must not be followed, or the ticket is spent
   on the SPA's own index page.
2. ``POST {plaza}/api/base/login {"ticket", "service"}`` → sets the
   ``datasets-session`` cookie and returns the caller's user record.
3. Every later call carries that cookie. The SPA also sends ``x-user-id``;
   calls succeed without it, but it is sent anyway to match the front end.

Responses are ``{"code": 0, "data": …, "msg": "…"}``. ``code`` is 0 on success
and non-zero for a declared failure whose reason is in ``msg``; the HTTP status
stays 200 for those, so success can never be read off the status code alone.
HTTP status still matters: redirects and 401 require authentication recovery,
and other HTTP errors are classified before business success. An unauthenticated
call answers
``401 {"code": 7, …, "msg": "未登录或非法访问"}``, which is the signal to run the
handshake again.

The handshake is two cheap requests, so the signed-in client is cached only
in its owning Transport and closed with it. CAS credentials live separately in ``cas_session.json``; plaza cookies stay in memory. A rejected plaza cookie is re-minted from CAS first; only a
second failure escalates to the transport's platform-session refresh ladder.
"""

from __future__ import annotations

from inspire.platform.web.flow import Program, workflow, http_call, call

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import quote

import requests

from inspire.platform.web.session import (
    TRANSIENT_HTTP_STATUSES,
    SessionExpiredError,
    TransientAPIError,
    WebSession,
)
from inspire.platform.web.session.retry import retry_after_seconds
from inspire.platform.web.session.cas import CasSession, ticket_from_response

if TYPE_CHECKING:
    from inspire.platform.web.transport import Transport

__all__ = [
    "CAS_BASE_URL",
    "PLAZA_BASE_URL",
    "PlazaError",
    "PlazaClient",
    "PlazaNotSignedIn",
    "PlazaUnavailable",
    "PlazaRejected",
    "sign_in",
    "unwrap",
    "plaza_request",
    "reset_plaza_client",
]

PLAZA_BASE_URL = "https://aip.sii.edu.cn"
CAS_BASE_URL = "https://cas.sii.edu.cn"

logger = logging.getLogger(__name__)


class PlazaError(ValueError):
    """A data plaza call failed, either in transport or in its response.

    Subclasses ``ValueError`` so the CLI's existing ``except ValueError``
    boundaries keep mapping a refused request to the same user-facing API
    error, exactly as the qz browser APIs do.
    """


class PlazaNotSignedIn(PlazaError):
    """The plaza does not recognize the ``datasets-session`` being presented."""


PLAZA_UNAVAILABLE_MESSAGE = "The data plaza needs a CAS login; run inspire login --cas."


class PlazaUnavailable(PlazaError):
    """The plaza cannot be signed in to with OAuth device tokens."""

    def __init__(self, message: str = PLAZA_UNAVAILABLE_MESSAGE) -> None:
        super().__init__(message)


class CasTicketExpired(PlazaUnavailable):
    """CAS refused a ticket; OAuth refresh cannot repair this session."""


def _login_hint(account: str | None) -> str:
    from inspire.accounts import default_account

    suffix = f" --account {account}" if account and account != default_account() else ""
    return f"inspire login --cas{suffix}"


@dataclass
class PlazaClient:
    """One signed-in HTTP session against the plaza."""

    http: requests.Session
    user_id: str

    def close(self) -> None:
        try:
            self.http.close()
        except Exception:  # pragma: no cover - closing must never raise
            logger.debug("Closing the data plaza session failed.", exc_info=True)


def _service_url() -> str:
    """The service the CAS ticket is minted for — the plaza's own index."""
    return f"{PLAZA_BASE_URL}/"


def reset_plaza_client() -> None:
    """Close and clear the active (or process-default) transport's plaza session.

    The next plaza call on that transport performs a fresh CAS handshake.
    Other transports and their signed-in sessions are unaffected.
    """
    from inspire.platform.web.runtime import get_transport

    get_transport().reset_plaza_client()


def _json_body(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise PlazaError("The data plaza answered with a non-JSON body.") from exc
    if not isinstance(payload, dict):
        raise PlazaError("The data plaza answered with an unexpected body.")
    return payload


@workflow
def sign_in(session: WebSession, transport: Transport, timeout: float = 30) -> Program[PlazaClient]:
    """Trade the account's stored CAS cookie for a plaza session."""
    stored = yield call(CasSession.load, session.account)
    if stored is None:
        raise PlazaUnavailable(
            f"The data plaza needs a CAS login; run {_login_hint(session.account)}."
        )
    http = yield call(stored.http, session.account)
    service = _service_url()
    try:
        ticket_response = yield http_call(
            http.get,
            f"{CAS_BASE_URL}/cas/login?service={quote(service, safe='')}",
            allow_redirects=False,
            timeout=min(timeout, transport.remaining()),
        )
        transport.check_deadline()
        _raise_transient(ticket_response)
        ticket = ticket_from_response(ticket_response)
        if not ticket:
            raise CasTicketExpired(f"CAS login expired; run {_login_hint(session.account)}.")

        login_response = yield http_call(
            http.post,
            f"{PLAZA_BASE_URL}/api/base/login",
            json={"ticket": ticket, "service": service},
            timeout=min(timeout, transport.remaining()),
            allow_redirects=False,
        )
        transport.check_deadline()
        _raise_transient(login_response)
        if login_response.status_code == 401 or login_response.status_code >= 400:
            raise SessionExpiredError("The data plaza rejected the CAS ticket.")
        payload = _json_body(login_response)
        if payload.get("code") != 0:
            raise SessionExpiredError("The data plaza declined the CAS sign-in.")

        data = payload.get("data")
        user = data.get("userInfo") if isinstance(data, dict) else None
        user_id = str((user or {}).get("ID") or "").strip()
        if user_id:
            http.headers["x-user-id"] = user_id
        return PlazaClient(http=http, user_id=user_id)
    except requests.RequestException as exc:
        http.close()
        transport.check_deadline()
        raise PlazaError("The data plaza could not be reached.") from exc
    except BaseException:
        http.close()
        raise


def _raise_transient(response: requests.Response) -> None:
    if response.status_code in TRANSIENT_HTTP_STATUSES:
        raise TransientAPIError(
            f"Data plaza returned {response.status_code}",
            status=response.status_code,
            retry_after=retry_after_seconds(response.headers),
        )


class PlazaRejected(PlazaError):
    """An HTTP or envelope rejection, retaining its status for write classification."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def unwrap(response: requests.Response) -> Any:
    """Return the response's ``data``, or raise what the plaza actually said.

    Order matters and mirrors the qz v2 discipline: throttling and server
    faults are judged before the body is read at all, because a rate limiter's
    answer is an error page rather than the JSON envelope.
    """
    _raise_transient(response)
    if 300 <= response.status_code < 400:
        raise PlazaNotSignedIn("The data plaza redirected the request to a login page.")
    if response.status_code == 401:
        payload = _json_body(response)
        raise PlazaNotSignedIn(str(payload.get("msg") or "Not signed in to the data plaza."))
    if response.status_code >= 400:
        raise PlazaRejected(
            f"Data plaza returned {response.status_code}.", status=response.status_code
        )

    payload = _json_body(response)
    if payload.get("code") != 0:
        raise PlazaRejected(str(payload.get("msg") or "The data plaza declined the request."))
    return payload.get("data")


def plaza_request(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    body: Optional[dict[str, Any]] = None,
    timeout: int = 30,
    session: Optional[WebSession] = None,
) -> Any:
    """Call one plaza endpoint and return its unwrapped ``data`` payload.

    Follows the same 401 discipline the qz browser APIs do. A lapsed
    ``datasets-session`` is re-minted from the caller's CAS cookie,
    and only when that fails too is the platform session itself refreshed —
    logging in again is expensive, and most expiries are the plaza's alone.
    """
    from inspire.platform.web.runtime import get_transport

    return get_transport(session).plaza_request(
        method, path, params=params, body=body, timeout=timeout
    )
