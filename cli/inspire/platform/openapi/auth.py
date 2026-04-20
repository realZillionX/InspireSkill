"""Authentication helpers for the Inspire OpenAPI client."""

from __future__ import annotations

import logging

from inspire.platform.openapi.errors import AuthenticationError

logger = logging.getLogger(__name__)


def authenticate(api, username: str, password: str) -> bool:  # noqa: ANN001
    """Authenticate and get access token."""
    api._validate_required_params(username=username, password=password)

    payload = {"username": username, "password": password}
    result = api._make_request("POST", api.endpoints.AUTH_TOKEN, payload)

    if result.get("code") == 0:
        token = result.get("data", {}).get("access_token")
        if token:
            api.token = token
            logger.info("✅ Authentication successful!")
            return True
        raise AuthenticationError("Authentication succeeded but no token returned")

    error_msg = result.get("message", "Authentication failed")
    raise AuthenticationError(f"❌ Authentication failed: {error_msg}")


def check_authentication(api) -> None:  # noqa: ANN001
    """Check if authenticated."""
    if not api.token:
        raise AuthenticationError(
            "Not authenticated. Please call authenticate() first or provide valid credentials."
        )


__all__ = ["authenticate", "check_authentication"]
