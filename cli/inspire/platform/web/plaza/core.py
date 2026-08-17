"""Session handshake and transport for 数据广场 on ``aip.sii.edu.cn``.

数据广场 (上海创智学院数据广场) is a different application from the qz console:
another host, another REST style, and its own session cookie. The qz console's
数据集 sidebar entry is only an external link to it, and it is the sole place the
dataset catalogue can be browsed or searched — qz's own ``/api/v2/dataset``
route carries a single ``ValidateDataset`` Action and no listing at all (see
:mod:`inspire.platform.web.browser_api.datasets`).

Signing in needs no browser. The CLI's web session already holds the CAS
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
The one status that does carry meaning is 401 — an unauthenticated call answers
``401 {"code": 7, …, "msg": "未登录或非法访问"}``, which is the signal to run the
handshake again.

The handshake is two cheap requests, so the signed-in client is cached in
process only; there is deliberately no on-disk counterpart to
``web_session.json`` to keep stale and account-crossing state out of the way.
"""

from __future__ import annotations

import atexit
import logging
import threading
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

import requests

from inspire.platform.web.session import (
    TRANSIENT_HTTP_STATUSES,
    SessionExpiredError,
    TransientAPIError,
    WebSession,
    build_requests_session,
    get_web_session,
    with_transient_retry,
)
from inspire.platform.web.session.retry import retry_after_seconds

__all__ = [
    "CAS_BASE_URL",
    "PLAZA_BASE_URL",
    "PlazaError",
    "plaza_request",
    "reset_plaza_client",
]

PLAZA_BASE_URL = "https://aip.sii.edu.cn"
CAS_BASE_URL = "https://cas.sii.edu.cn"

logger = logging.getLogger(__name__)


class PlazaError(ValueError):
    """数据广场 answered, and the answer was a declared failure.

    Subclasses ``ValueError`` so the CLI's existing ``except ValueError``
    boundaries keep mapping a refused request to the same user-facing API
    error, exactly as the qz browser APIs do.
    """


class _PlazaNotSignedIn(PlazaError):
    """The plaza does not recognize the ``datasets-session`` being presented."""


@dataclass
class _PlazaClient:
    """One signed-in HTTP session against the plaza."""

    http: requests.Session
    user_id: str

    def close(self) -> None:
        try:
            self.http.close()
        except Exception:  # pragma: no cover - closing must never raise
            logger.debug("Closing the data plaza session failed.", exc_info=True)


_client_lock = threading.Lock()
_client: Optional[_PlazaClient] = None
_client_key: Optional[tuple[str, float]] = None


def _service_url() -> str:
    """The service the CAS ticket is minted for — the plaza's own index."""
    return f"{PLAZA_BASE_URL}/"


def _session_key(session: WebSession) -> tuple[str, float]:
    """Identify the web session a cached plaza client was derived from.

    Keying on the account *and* the session's creation time means a refreshed
    or switched-to session never reuses another one's ``datasets-session``.
    """
    return (
        str(getattr(session, "account", "") or ""),
        float(getattr(session, "created_at", 0.0) or 0.0),
    )


def reset_plaza_client() -> None:
    """Drop the cached plaza session so the next call signs in again."""
    global _client, _client_key

    with _client_lock:
        stale, _client, _client_key = _client, None, None
    if stale is not None:
        stale.close()


atexit.register(reset_plaza_client)


def _json_body(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise PlazaError("The data plaza answered with a non-JSON body.") from exc
    if not isinstance(payload, dict):
        raise PlazaError("The data plaza answered with an unexpected body.")
    return payload


def _cas_ticket(location: str) -> str:
    """Read the service ticket out of the CAS redirect target."""
    _, separator, query = str(location or "").partition("?")
    if not separator:
        return ""
    for field in query.split("&"):
        name, _, value = field.partition("=")
        if name == "ticket":
            return value.strip()
    return ""


def _sign_in(session: WebSession) -> _PlazaClient:
    """Trade the web session's CAS cookie for a plaza ``datasets-session``."""
    http = build_requests_session(session, PLAZA_BASE_URL)
    service = _service_url()
    try:
        ticket_response = http.get(
            f"{CAS_BASE_URL}/cas/login?service={quote(service, safe='')}",
            allow_redirects=False,
            timeout=30,
        )
        ticket = _cas_ticket(ticket_response.headers.get("Location", ""))
        if not ticket:
            # No ticket means CAS did not recognize the cookie: the platform
            # session behind it is what expired, not the plaza's.
            raise SessionExpiredError("CAS issued no data plaza ticket for this session.")

        login_response = http.post(
            f"{PLAZA_BASE_URL}/api/base/login",
            json={"ticket": ticket, "service": service},
            timeout=30,
            allow_redirects=False,
        )
        if login_response.status_code == 401 or login_response.status_code >= 400:
            raise SessionExpiredError("The data plaza rejected the CAS ticket.")
        payload = _json_body(login_response)
        if payload.get("code") != 0:
            raise SessionExpiredError(
                f"The data plaza declined the sign-in: {payload.get('msg') or 'unknown reason'}"
            )

        data = payload.get("data")
        user = data.get("userInfo") if isinstance(data, dict) else None
        user_id = str((user or {}).get("ID") or "").strip()
        if user_id:
            http.headers["x-user-id"] = user_id
        return _PlazaClient(http=http, user_id=user_id)
    except requests.RequestException as exc:
        http.close()
        raise PlazaError("The data plaza could not be reached.") from exc
    except BaseException:
        http.close()
        raise


def _client_for(session: WebSession) -> _PlazaClient:
    """Return the signed-in client for *session*, signing in when needed."""
    global _client, _client_key

    key = _session_key(session)
    with _client_lock:
        if _client is not None and _client_key == key:
            return _client
    signed_in = _sign_in(session)
    with _client_lock:
        stale, _client, _client_key = _client, signed_in, key
    if stale is not None:
        stale.close()
    return signed_in


def _unwrap(response: requests.Response) -> Any:
    """Return the response's ``data``, or raise what the plaza actually said.

    Order matters and mirrors the qz v2 discipline: throttling and server
    faults are judged before the body is read at all, because a rate limiter's
    answer is an error page rather than the JSON envelope.
    """
    if response.status_code in TRANSIENT_HTTP_STATUSES:
        raise TransientAPIError(
            f"Data plaza returned {response.status_code}",
            status=response.status_code,
            retry_after=retry_after_seconds(response.headers),
        )
    if 300 <= response.status_code < 400:
        raise _PlazaNotSignedIn("The data plaza redirected the request to a login page.")
    if response.status_code == 401:
        payload = _json_body(response)
        raise _PlazaNotSignedIn(str(payload.get("msg") or "Not signed in to the data plaza."))
    if response.status_code >= 400:
        raise PlazaError(f"Data plaza returned {response.status_code}.")

    payload = _json_body(response)
    if payload.get("code") != 0:
        raise PlazaError(str(payload.get("msg") or "The data plaza declined the request."))
    return payload.get("data")


def _call(
    client: _PlazaClient,
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]],
    body: Optional[dict[str, Any]],
    timeout: int,
) -> Any:
    url = f"{PLAZA_BASE_URL}{path}"

    def _once() -> Any:
        try:
            response = client.http.request(
                method.upper(),
                url,
                params=params,
                json=body,
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise PlazaError("The data plaza did not answer.") from exc
        return _unwrap(response)

    return with_transient_retry(_once)


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
    ``datasets-session`` is re-minted from the CAS cookie the CLI already holds,
    and only when that fails too is the platform session itself refreshed —
    logging in again is expensive, and most expiries are the plaza's alone.
    """
    active = session if session is not None else get_web_session()
    last: BaseException | None = None
    for attempt in range(3):
        if attempt:
            # Whatever was cached did not authenticate; never retry with it.
            reset_plaza_client()
        if attempt == 2:
            # The CAS cookie itself is stale, so nothing derived from it will
            # authenticate either. Rebuild the platform session first.
            active = get_web_session(
                force_refresh=True,
                account=getattr(active, "account", None),
            )
        try:
            return _call(
                _client_for(active),
                method,
                path,
                params=params,
                body=body,
                timeout=timeout,
            )
        except (SessionExpiredError, _PlazaNotSignedIn) as exc:
            logger.debug("Data plaza call was not authenticated (attempt %d).", attempt + 1)
            last = exc
    raise SessionExpiredError(
        "The data plaza rejected the refreshed platform session."
    ) from last
