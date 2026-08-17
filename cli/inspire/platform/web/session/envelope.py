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

# v2 carries throttling and server faults in the envelope, under HTTP 200.
# An error announced this way is still the platform declining to answer, and
# must not read as "the answer is empty".
_TRANSIENT_V2_ERROR_CODES = frozenset(
    {
        "internalerror",
        "internalfailure",
        "internalservererror",
        "requesttimeout",
        "serviceunavailable",
        "slowdown",
        "throttling",
        "throttlingexception",
        "toomanyrequests",
        "toomanyrequestsexception",
    }
)


def _is_transient_v2_error_code(code: str) -> bool:
    return str(code or "").strip().lower().replace("_", "") in _TRANSIENT_V2_ERROR_CODES


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
            text = f"API error: {code}: {message}"
            if _is_transient_v2_error_code(code):
                raise TransientAPIError(text)
            raise ValueError(text)
    elif data.get("code") not in (None, 0):
        raise ValueError(f"API error: {data.get('message')}")

    payload = data.get("Result")
    if isinstance(payload, dict):
        return payload
    if payload is None:
        nested_payload = data.get("data")
        if isinstance(nested_payload, dict):
            return nested_payload
    return {}
