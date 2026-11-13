"""Official device authorization protocol; never follows authentication redirects."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import requests

from inspire import __version__
from .models import AuthenticationError, RefreshRejectedError, is_offline_refresh_token

CLIENT_SOURCE = f"inspire-cli/inspireskill-{__version__}"
DEVICE_SCOPE = "openid profile email offline_access"
# Informational only: Keycloak controls the real offline session idle timeout.
OFFLINE_IDLE_ESTIMATE_SECONDS = 30 * 24 * 60 * 60
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


@dataclass(frozen=True)
class TokenResponse:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    access_expires_at: float
    refresh_expires_at: float
    refresh_offline: bool = False


def platform_headers(base_url: str, target_url: str, access_token: str) -> dict[str, str]:
    """Return credentials only for the configured platform origin."""
    base = urlsplit(base_url)
    target = urlsplit(target_url)
    if (base.scheme, base.hostname, base.port) != (target.scheme, target.hostname, target.port):
        return {}
    return {
        "Authorization": f"Bearer {access_token}",
        "X-Inspire-Client-Source": CLIENT_SOURCE,
    }


class DeviceAuthorization:
    """Explicit device login with injectable HTTP, time, and user notification."""

    def __init__(
        self,
        base_url: str,
        http: requests.Session,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = http
        self.clock = clock
        self.sleep = sleep

    def _post(
        self, endpoint: str, payload: dict[str, str], *, deadline: float | None = None
    ) -> tuple[int, dict[str, Any]]:
        from inspire.platform.web.runtime import active_transport

        owner = active_transport.get()
        timeout = min(30.0, owner.remaining()) if owner is not None else 30.0
        if deadline is not None:
            timeout = min(timeout, deadline - self.clock())
        if timeout <= 0:
            raise AuthenticationError("Device authorization expired; run inspire login.")
        response = self.http.post(
            f"{self.base_url}/api/v1/public/auth/{endpoint}",
            json=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=timeout,
            allow_redirects=False,
        )
        try:
            body = response.json()
        except ValueError:
            raise AuthenticationError(
                "Invalid authentication response; run inspire login."
            ) from None
        if not isinstance(body, dict):
            raise AuthenticationError("Invalid authentication response; run inspire login.")
        return response.status_code, body

    @staticmethod
    def _tokens(body: dict[str, Any]) -> TokenResponse:
        data = body.get("data")
        if not isinstance(data, dict):
            raise AuthenticationError("Missing authentication tokens; run inspire login.")
        try:
            access = data["access_token"]
            refresh = data["refresh_token"]
            if (
                not isinstance(access, str)
                or not access
                or not isinstance(refresh, str)
                or not refresh
            ):
                raise ValueError
            refresh_lifetime = float(data.get("refresh_expires_in", 0))
            offline = refresh_lifetime == 0 or is_offline_refresh_token(refresh)
            now = time.time()
            return TokenResponse(
                access,
                refresh,
                now + float(data["expires_in"]),
                now + (OFFLINE_IDLE_ESTIMATE_SECONDS if offline else refresh_lifetime),
                refresh_offline=offline,
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            raise AuthenticationError("Invalid authentication tokens; run inspire login.") from None

    def login(
        self, notify: Callable[[str, str, float], None], *, deadline: float | None = None
    ) -> TokenResponse:
        """Request a device code, notify the caller, and wait for authorization."""
        from inspire.platform.web.runtime import active_transport

        owner = active_transport.get()
        if owner is not None:
            transport_deadline = self.clock() + owner.remaining()
            deadline = min(deadline, transport_deadline) if deadline is not None else transport_deadline
        status, body = self._post(
            "device-code", {"scope": DEVICE_SCOPE}, deadline=deadline
        )
        if status != 200 or body.get("code") != 0:
            raise AuthenticationError("Device authorization failed; run inspire login.")
        try:
            data = body["data"]
            code = data["device_code"]
            user_code = data["user_code"]
            uri = data.get("verification_uri_complete") or data["verification_uri"]
            expires = float(data["expires_in"])
            interval = max(1.0, float(data.get("interval", 5)))
            if not all(isinstance(value, str) and value for value in (code, user_code, uri)):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise AuthenticationError("Invalid device authorization response.") from None
        expires_at = self.clock() + expires
        deadline = min(deadline, expires_at) if deadline is not None else expires_at
        notify(uri, user_code, expires)
        while self.clock() < deadline:
            self.sleep(min(interval, max(0, deadline - self.clock())))
            if self.clock() >= deadline:
                break
            status, body = self._post(
                "token", {"device_code": code, "grant_type": DEVICE_GRANT}, deadline=deadline
            )
            if status == 200 and body.get("code") == 0:
                return self._tokens(body)
            error = body.get("error") or body.get("message")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            # Do not reflect server text: it may contain credentials.
            if error in {"expired_token", "access_denied", "invalid_grant"}:
                raise AuthenticationError(f"Device authorization {error}; run inspire login.")
            raise AuthenticationError("Device authorization failed; run inspire login.")
        raise AuthenticationError("Device authorization expired; run inspire login.")

    def refresh(self, refresh_token: str) -> TokenResponse:
        """Exchange a refresh token for a new rotating token pair."""
        status, body = self._post("refresh", {"refresh_token": refresh_token})
        if status != 200 or body.get("code") != 0:
            raise RefreshRejectedError(body.get("message"), status)
        return self._tokens(body)
