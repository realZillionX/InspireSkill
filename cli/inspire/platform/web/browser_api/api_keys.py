"""Account API keys; secrets never enter metadata or exception messages."""

from __future__ import annotations

from inspire.platform.errors import SubmissionUncertainError

from dataclasses import dataclass, field
from typing import Any

from inspire.platform.web.browser_api.core import _get_base_url, _request_json, _v2_result
from inspire.platform.web.session import SessionExpiredError, WebSession, get_web_session


@dataclass(frozen=True)
class APIKeyInfo:
    key_id: str = field(repr=False)
    name: str
    created_at: str


def _call(action: str, body: dict[str, Any], session: WebSession) -> dict[str, Any]:
    try:
        return _v2_result(
            _request_json(
                session,
                "POST",
                f"/api/v2/user?Action={action}",
                referer=f"{_get_base_url()}/jobs/modelDeployment",
                body=body,
            )
        )
    except SessionExpiredError:
        raise SessionExpiredError("API key operation requires a valid account session.") from None
    except SubmissionUncertainError as error:
        error.inspect = "API keys"
        error.args = (str(SubmissionUncertainError(error.operation_id, inspect="API keys")),)
        error.__cause__ = None
        error.__context__ = None
        raise error from None
    except Exception as error:
        # Preserve classification and metadata, but never expose server text or
        # its cause chain: either can contain plaintext keys/internal handles.
        error.args = (f"API key operation {action} failed; no secret was displayed.",)
        error.__cause__ = None
        error.__context__ = None
        raise error from None


def list_api_keys(*, session: WebSession | None = None) -> list[APIKeyInfo]:
    result = _call("GetMyAPIList", {}, session or get_web_session())
    items = result.get("items")
    if not isinstance(items, list):
        raise ValueError("API key list returned an invalid response.")
    keys = []
    for item in items:
        if not isinstance(item, dict) or not all(
            isinstance(item.get(k), str) and item[k] for k in ("key_id", "name")
        ):
            raise ValueError("API key list returned an invalid entry.")
        keys.append(APIKeyInfo(item["key_id"], item["name"], str(item.get("created_at") or "")))
    return keys


def create_api_key(name: str, *, session: WebSession | None = None) -> None:
    _call("GenerateAPIKey", {"key_name": name}, session or get_web_session())


def delete_api_key(key_id: str, *, session: WebSession | None = None) -> None:
    _call("DeleteAPIKey", {"api_key_id": key_id}, session or get_web_session())


def get_api_key_plaintext(key_id: str, *, session: WebSession | None = None) -> str:
    result = _call("GetAPIKeyPlaintext", {"api_key_id": key_id}, session or get_web_session())
    value = result.get("value")
    if (
        not isinstance(value, str)
        or not value
        or any(c.isspace() for c in value)
        or "\x00" in value
        or "*" in value
    ):
        raise ValueError("API key plaintext was unavailable or invalid.")
    return value
