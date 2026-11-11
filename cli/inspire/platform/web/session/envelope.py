"""The `/api/v2` response envelope.

This lives in the session layer rather than in `browser_api/` because login
needs it too: the very first two calls of a session — the "am I authenticated"
probe and workspace discovery — are v2 Actions, and they arrive in the same
envelope as everything after them. There is exactly one unwrapper for all of
it; a second one written by hand is how `code != 0` turned every real error
into `API error: None` once already.
"""

from __future__ import annotations

from typing import Any

from inspire.platform.web.session.models import TransientAPIError
from inspire.platform.errors import APIError
from inspire.services.utils.json_formatter import sanitize_text

# v2 carries throttling and server faults in the envelope, under HTTP 200.
# An error announced this way is still the platform declining to answer, and
# must not read as "the answer is empty".
_SERVER_V2_ERROR_CODES = frozenset(
    {
        "internalerror",
        "internalfailure",
        "internalservererror",
        "requesttimeout",
        "serviceunavailable",
    }
)
_THROTTLING_V2_ERROR_CODES = frozenset(
    {
        "slowdown",
        "throttling",
        "throttlingexception",
        "toomanyrequests",
        "toomanyrequestsexception",
    }
)

_TRANSIENT_V2_ERROR_CODES = _SERVER_V2_ERROR_CODES | _THROTTLING_V2_ERROR_CODES


def _is_transient_v2_error_code(code: str) -> bool:
    return str(code or "").strip().lower().replace("_", "") in _TRANSIENT_V2_ERROR_CODES


class _ResponseEnvelope(dict[str, Any]):
    """Keep observed HTTP context outside the platform's JSON keys."""

    def __init__(self, data: dict[str, Any], **context: Any) -> None:
        super().__init__(data)
        self.context = context


def _v2_error_metadata(data: dict[str, Any]) -> dict[str, Any]:
    metadata = data.get("ResponseMetadata")
    result = dict(data.context) if isinstance(data, _ResponseEnvelope) else {}
    if isinstance(metadata, dict):
        error = metadata.get("Error")
        for name, value in (
            ("code", error.get("Code") if isinstance(error, dict) else None),
            ("service", metadata.get("Service")),
            ("action", metadata.get("Action")),
            ("request_id", metadata.get("RequestId")),
        ):
            if isinstance(value, str):
                result[name] = value
    return result


def _response_metadata(status: int, headers: Any, data: Any) -> dict[str, Any]:
    """Extract the same scalar context for HTTP and browser responses."""
    from inspire.platform.web.session.retry import retry_after_seconds

    context: dict[str, Any] = {"status": status, "retry_after": retry_after_seconds(headers)}
    if isinstance(data, dict):
        context.update(_v2_error_metadata(data))
    return context


def _v2_result(data: dict[str, Any]) -> dict[str, Any]:
    """Unwrap the `/api/v2` AWS-style envelope.

    v2 reports business errors inside ``ResponseMetadata.Error`` while the HTTP
    status stays 200, so success can never be inferred from the status code.
    Callers pick their own list key out of the result; there is no cross-Action
    convention for it.

    The ``code``/``data`` branch is a shim, not a live shape: no Action known to
    this client answers that way. It stays because the failure mode of dropping
    it is silent — an Action that did answer that way would return ``{}`` and
    read as "the platform has none of these" — and 114 Actions is more than has
    been checked one by one.
    """
    metadata = data.get("ResponseMetadata")
    if isinstance(metadata, dict):
        error = metadata.get("Error")
        if isinstance(error, dict):
            code = error.get("Code") or "Error"
            message = error.get("Message") or "unknown error"
            text = f"API error: {sanitize_text(code)}: {sanitize_text(message)}"
            if _is_transient_v2_error_code(code):
                raise TransientAPIError(text, **_v2_error_metadata(data))
            raise APIError(text, **_v2_error_metadata(data))
    elif data.get("code") not in (None, 0):
        raise APIError(f"API error: {sanitize_text(data.get('message'))}", **_v2_error_metadata(data))

    payload = data.get("Result")
    if isinstance(payload, dict):
        return _ResponseEnvelope(payload, **_v2_error_metadata(data))
    if payload is None:
        nested_payload = data.get("data")
        if isinstance(nested_payload, dict):
            return _ResponseEnvelope(nested_payload, **_v2_error_metadata(data))
    return _ResponseEnvelope({}, **_v2_error_metadata(data))
