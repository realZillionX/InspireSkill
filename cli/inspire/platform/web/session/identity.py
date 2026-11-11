"""Read display identity from an access token without validating authentication."""

from __future__ import annotations

import base64
import json


def token_login_username(access_token: str) -> str | None:
    """Return the JWT's display login name; never use claims for authorization."""
    try:
        encoded = access_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (ValueError, IndexError, UnicodeError):
        return None
    value = claims.get("preferred_username") if isinstance(claims, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None
