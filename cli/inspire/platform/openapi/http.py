"""HTTP request helpers for the Inspire OpenAPI client."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

import requests

from inspire.platform.openapi.errors import InspireAPIError

logger = logging.getLogger(__name__)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.strip().lower()
    if lowered in {"authorization", "cookie", "set-cookie"}:
        return True
    return any(
        token in lowered
        for token in ("password", "passwd", "token", "secret", "api_key", "apikey", "auth")
    )


def _sanitize_for_log(value: Any, *, key_hint: str | None = None) -> Any:
    if key_hint and _is_sensitive_key(key_hint):
        return "<redacted>"

    if isinstance(value, dict):
        return {k: _sanitize_for_log(v, key_hint=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_for_log(item) for item in value]
    return value


def make_request_with_retry(
    api, method: str, url: str, **kwargs
) -> requests.Response:  # noqa: ANN001
    """Request method with retry mechanism."""
    last_exception = None
    # Add SSL verification setting to kwargs if not already present
    if "verify" not in kwargs:
        kwargs["verify"] = api.config.verify_ssl

    for attempt in range(api.config.max_retries + 1):
        try:
            if method.upper() == "POST":
                response = api.session.post(url, timeout=api.config.timeout, **kwargs)
            else:
                response = api.session.get(url, timeout=api.config.timeout, **kwargs)

            if response.status_code < 500 and response.status_code != 429:
                return response

            # 429 Too Many Requests — retry with backoff (documented rate limit).
            if response.status_code == 429:
                if attempt < api.config.max_retries:
                    wait = api.config.retry_delay * (attempt + 1)
                    logger.warning(
                        "Rate limited (429), retrying in %ss...",
                        wait,
                    )
                    time.sleep(wait)
                    continue
                # Exhausted retries; fall through to raise_for_status.

            # Check if server returned an API error in JSON body (don't retry these)
            try:
                error_body = response.json()
                error_code = error_body.get("code")
                error_msg = error_body.get("message", "")
                if error_code is not None and error_code != 0:
                    # This is an API-level error, not a transient server error
                    # Don't retry - return immediately so caller can handle it
                    logger.warning(
                        "API error %s: %s (HTTP %s)",
                        error_code,
                        error_msg,
                        response.status_code,
                    )
                    return response
            except (ValueError, KeyError):
                pass  # Not JSON or missing fields, treat as normal 500

            if attempt < api.config.max_retries:
                logger.warning(
                    "Server error %s, retrying in %ss...",
                    response.status_code,
                    api.config.retry_delay,
                )
                time.sleep(api.config.retry_delay * (attempt + 1))
                continue

            response.raise_for_status()

        except requests.exceptions.Timeout as e:
            last_exception = e
            if attempt < api.config.max_retries:
                logger.warning("Request timeout, retrying in %ss...", api.config.retry_delay)
                time.sleep(api.config.retry_delay * (attempt + 1))
                continue
            raise InspireAPIError(f"Request timeout after {api.config.max_retries} retries")

        except requests.exceptions.ConnectionError as e:
            last_exception = e
            if attempt < api.config.max_retries:
                logger.warning("Connection error, retrying in %ss...", api.config.retry_delay)
                time.sleep(api.config.retry_delay * (attempt + 1))
                continue
            raise InspireAPIError(f"Connection error after {api.config.max_retries} retries")

    # Should not reach here
    raise InspireAPIError(f"Request failed: {str(last_exception)}")


def make_request(
    api,  # noqa: ANN001
    method: str,
    endpoint: str,
    payload: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Make authenticated request to API."""
    url = f"{api.base_url}{endpoint}"

    # Add auth header if token exists
    headers = api.headers.copy()
    if api.token:
        headers["Authorization"] = f"Bearer {api.token}"

    # Log request details
    logger.debug("API Request: %s %s", method, url)
    logger.debug("Headers: %s", json.dumps(_sanitize_for_log(headers), ensure_ascii=False))
    if payload:
        logger.debug("Payload: %s", json.dumps(_sanitize_for_log(payload), ensure_ascii=False))

    try:
        if method.upper() == "POST":
            response = make_request_with_retry(
                api,
                method,
                url,
                headers=headers,
                json=payload,
            )
        else:
            response = make_request_with_retry(api, method, url, headers=headers)

        # Try to parse JSON response
        try:
            result = response.json()
        except json.JSONDecodeError:
            body_preview = (response.text or "")[: api.ERROR_BODY_PREVIEW_LIMIT]
            raise InspireAPIError(
                "Failed to parse API response as JSON.\n"
                f"HTTP {response.status_code} from {url}\n"
                f"Body (first {api.ERROR_BODY_PREVIEW_LIMIT} chars): {body_preview}"
            )

        return result

    except requests.exceptions.RequestException as e:
        raise InspireAPIError(f"API request failed: {str(e)}") from e


__all__ = ["make_request", "make_request_with_retry"]
