"""Response mapping shared by the requests and httpx adapters."""

import json

from typing import Any, NoReturn
from dataclasses import dataclass
from inspire.platform.errors import AuthenticationError, ValidationError
from inspire.platform.web.session.envelope import _ResponseEnvelope, _v2_error_metadata


def _response_metadata(response: Any, data: Any) -> dict[str, Any]:
    from inspire.platform.web.session.retry import retry_after_seconds

    context: dict[str, Any] = {"status": response.status_code}
    headers = getattr(response, "headers", {})
    context["retry_after"] = retry_after_seconds(headers)
    if isinstance(data, dict):
        context.update(_v2_error_metadata(data))
    return context


class _NonJSONResponse(ValueError):
    """Only a decoded HTTP body failure is eligible for CLI browser fallback."""


class _SDKResponsePolicy:
    def response(self, response: Any) -> Any:
        from inspire.platform.web.session.models import SessionExpiredError, TransientAPIError

        try:
            data = response.json()
        except (ValueError, AttributeError):
            if response.status_code < 300:
                raise
            data = None
        context = _response_metadata(response, data)
        if response.status_code == 401 or 300 <= response.status_code < 400:
            raise SessionExpiredError("Authentication expired.", **context)
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientAPIError("Temporary platform failure.", **context)
        if response.status_code == 403:
            raise AuthenticationError("Platform access denied.", **context)
        if response.status_code >= 400:
            raise ValidationError(f"HTTP {response.status_code}: {response.text[:500]}", **context)
        return _ResponseEnvelope(data, **context) if isinstance(data, dict) else data

    def browser_error(self, error: Exception) -> NoReturn:
        from inspire.platform.web.session.browser_client import _BrowserHTTPError
        from inspire.platform.web.session.models import TransientAPIError

        if isinstance(error, _BrowserHTTPError):
            context: dict[str, Any] = {"status": error.status}
            try:
                data = json.loads(error.body)
            except ValueError:
                data = None
            if isinstance(data, dict):
                context.update(_v2_error_metadata(data))
            if error.status == 403:
                raise AuthenticationError(str(error), **context) from error
            if error.status == 429 or error.status >= 500:
                raise TransientAPIError(str(error), **context) from error
            raise ValidationError(f"HTTP {error.status}: {error.body[:500]}", **context) from error
        raise error


class _CLIResponsePolicy(_SDKResponsePolicy):
    def response(self, response: Any) -> Any:
        from inspire.platform.web import session as web_session

        if response.status_code == 401 or 300 <= response.status_code < 400:
            raise web_session.SessionExpiredError("Session expired or invalid")
        if response.status_code >= 400:
            message = f"API returned {response.status_code}: {response.text}"
            if response.status_code in web_session.TRANSIENT_HTTP_STATUSES:
                raise web_session.TransientAPIError(
                    message,
                    status=response.status_code,
                    retry_after=web_session.retry_after_seconds(response.headers),
                )
            raise ValueError(message)
        try:
            return response.json()
        except ValueError as error:
            raise _NonJSONResponse(str(error)) from error

    def browser_error(self, error: Exception) -> NoReturn:
        from inspire.platform.web import session as web_session

        if web_session.is_playwright_browser_runtime_error(error):
            web_session.close_browser_client()
            web_session.raise_browser_runtime_error(error)
        raise error


_SDK_POLICY = _SDKResponsePolicy()
_CLI_POLICY = _CLIResponsePolicy()


def is_request_error(error: Exception) -> bool:
    """Normalize library exception families beside response mapping, not in drivers."""
    from requests.exceptions import RequestException
    import httpx

    return isinstance(error, (RequestException, httpx.RequestError))


@dataclass(frozen=True)
class HTTPOptions:
    method: str
    headers: dict[str, str]
    body: Any
    include_json: bool
    connect_timeout: float


def http_options(
    method: str,
    body: Any,
    referer: str | None,
    base_url: str,
    timeout: float,
    cli_compat: bool,
) -> HTTPOptions:
    """One wire contract; adapters only translate it to their library's arguments."""
    headers = {"Referer": referer} if referer else {}
    if cli_compat:
        upper = method.upper()
        if upper not in {"GET", "POST", "DELETE"}:
            raise ValueError(f"Unsupported HTTP method: {method}")
        if upper == "POST":
            headers["Content-Type"] = "application/json"
        return HTTPOptions(
            upper, headers, (body or {}) if upper == "POST" else None, upper == "POST", timeout
        )
    headers["Referer"] = referer or base_url + "/jobs/distributedTraining"
    return HTTPOptions(method, headers, body, True, min(10, timeout))


def classify_application_response(response: Any) -> None:
    """Share authentication and retry classification without decoding HTML/204."""
    from inspire.platform.web.session.models import SessionExpiredError, TransientAPIError
    from inspire.platform.web.session.retry import retry_after_seconds

    if response.status_code == 401 or 300 <= response.status_code < 400:
        raise SessionExpiredError("Authentication expired.")
    if response.status_code == 429 or response.status_code >= 500:
        raise TransientAPIError(
            "Temporary application failure.", status=response.status_code,
            retry_after=retry_after_seconds(response.headers),
        )
    if response.status_code == 403:
        raise AuthenticationError("Application access denied.")
    if response.status_code >= 400:
        raise ValidationError(f"HTTP {response.status_code}: {response.text[:500]}")
