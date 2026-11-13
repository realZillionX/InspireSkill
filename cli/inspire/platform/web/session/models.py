"""Web session models and cache persistence.

Storage: ``~/.inspire/accounts/<active>/web_session.json``, colocated with
the account's ``config.toml`` and ``bridges.json``. Switching account
switches session cache in lockstep.
"""

from __future__ import annotations

from inspire.local_files import atomic_write_text
from inspire.platform.errors import APIError, _ErrorMetadata

from inspire.platform.web.flow import blocking_io

import base64
import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from inspire.accounts.cache_lock import exclusive_cache_lock
from .identity import token_login_username

SESSION_TTL = 3600  # 1 hour


def is_offline_refresh_token(token: object) -> bool:
    """Classify a refresh JWT by its unverified ``typ``; never used to authenticate.

    Older releases rewrite an offline session without ``refresh_offline``, so
    the token itself is the durable source of truth for its kind.
    """
    if not isinstance(token, str):
        return False
    try:
        encoded = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (ValueError, IndexError, UnicodeError):
        return False
    return isinstance(claims, dict) and claims.get("typ") == "Offline"


class SessionExpiredError(_ErrorMetadata):
    """Raised when the web session has expired (401 from server)."""


class AuthenticationError(APIError):
    """Authentication requires an explicit device login."""


class RefreshRejectedError(AuthenticationError):
    """A refresh rejection with only a safe OAuth code and HTTP status."""

    def __init__(self, reason: object, status: int) -> None:
        self.reason = (
            reason if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,64}", reason)
            else "refresh_rejected"
        )
        super().__init__(
            f"Session refresh rejected by server ({self.reason}, HTTP {status}); run inspire login.",
            status=status,
        )


# Statuses that say the platform did not answer, not that the answer is no.
# 429 is the one seen in the wild: the browser APIs answer one
# ``(workspace, compute group)`` per request, so any workspace-wide fan-out
# runs straight into the rate limiter.
TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class TransientAPIError(APIError):
    """A request the platform refused for a reason that will pass.

    Rate limiting, request timeouts and server faults carry no information
    about whether a resource exists, how many there are, or whether a compute
    group has quotas. Treating one as data is how an empty list ends up cached
    as fact.

    It subclasses ``ValueError`` because that is what this whole layer already
    raises for a failed request, so every existing ``except ValueError``
    boundary keeps mapping it to the same user-facing API error. What it adds
    is the ability for the few places that *decide* something from a response
    to tell "the platform said no" apart from "the platform did not answer".
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        code: str | None = None,
        service: str | None = None,
        action: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(
            message, status=status, retry_after=retry_after, code=code,
            service=service, action=action, request_id=request_id,
        )


_API_STATUS_RE = re.compile(r"\bAPI returned (\d{3})\b")


def is_transient_api_error(error: BaseException) -> bool:
    """Whether *error* is a platform failure that says nothing about the data.

    Recognizes the typed error and, for callers holding an exception raised
    outside the transport (a caller-supplied loader, a test double), the
    ``API returned <status>: …`` text the transport has always produced.
    """
    if isinstance(error, TransientAPIError):
        return True
    match = _API_STATUS_RE.search(str(error or ""))
    if match is None:
        return False
    return int(match.group(1)) in TRANSIENT_HTTP_STATUSES


# Sentinel for "workspace not yet detected from the authenticated browser session".
DEFAULT_WORKSPACE_ID = "ws-00000000-0000-0000-0000-000000000000"


def _resolve_account_for_storage(explicit: Optional[str]) -> Optional[str]:
    """Pick the account whose directory holds the session cache.

    Order (mirrors the tunnel config resolver):
      1. *explicit* parameter
      2. ``~/.inspire/current`` via :mod:`inspire.accounts`
      3. ``None`` — no session cache is read or written (caller must login)
    """
    candidate = (explicit or "").strip()
    if candidate:
        return candidate
    try:
        from inspire.accounts import current_account
    except ImportError:  # pragma: no cover - accounts ships with the CLI
        return None
    return current_account()


def get_session_cache_file(account: Optional[str] = None) -> Optional[Path]:
    """Resolve the on-disk path for the session cache, or ``None``."""
    name = _resolve_account_for_storage(account)
    if not name:
        return None
    return Path.home() / ".inspire" / "accounts" / name / "web_session.json"


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError):
        return False


def _is_string_list(value: object) -> bool:
    return value is None or (
        isinstance(value, list) and all(isinstance(item, str) for item in value)
    )


def _is_string_mapping(value: object) -> bool:
    return value is None or (
        isinstance(value, dict)
        and all(isinstance(key, str) and isinstance(item, str) for key, item in value.items())
    )


def _is_bool_mapping(value: object) -> bool:
    return value is None or (
        isinstance(value, dict)
        and all(isinstance(key, str) and isinstance(item, bool) for key, item in value.items())
    )


def _is_valid_session_cache_payload(data: object) -> bool:
    """Return whether a decoded cache has the shape required by its consumers."""
    if not isinstance(data, dict):
        return False
    if not _is_finite_number(data.get("created_at")):
        return False

    if not all(
        isinstance(data.get(key), str) and data[key] for key in ("access_token", "refresh_token")
    ):
        return False
    if not all(
        _is_finite_number(data.get(key)) for key in ("access_expires_at", "refresh_expires_at")
    ):
        return False

    if not isinstance(data.get("refresh_offline", False), bool):
        return False

    for key in ("workspace_id", "login_username", "base_url", "account"):
        if data.get(key) is not None and not isinstance(data[key], str):
            return False
    if data.get("user_detail") is not None and not isinstance(data["user_detail"], dict):
        return False
    if not _is_string_list(data.get("all_workspace_ids")):
        return False
    if not _is_string_mapping(data.get("all_workspace_names")):
        return False
    return _is_bool_mapping(data.get("all_workspace_fair_scheduling"))


@dataclass
class WebSession:
    """Account-bound OAuth tokens and discovered platform metadata."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    access_expires_at: float
    refresh_expires_at: float
    created_at: float
    workspace_id: Optional[str] = None
    login_username: Optional[str] = None
    base_url: Optional[str] = None
    user_detail: Optional[dict[str, Any]] = None
    all_workspace_ids: Optional[list[str]] = None
    all_workspace_names: Optional[dict[str, str]] = None
    all_workspace_fair_scheduling: Optional[dict[str, bool]] = None
    account: Optional[str] = None
    refresh_offline: bool = False

    def is_valid(self) -> bool:
        """Check if session is still valid (not expired)."""
        return bool(self.access_token) and self.access_expires_at > time.time() + 60

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "access_expires_at": self.access_expires_at,
            "refresh_expires_at": self.refresh_expires_at,
            "refresh_offline": self.refresh_offline,
            "workspace_id": self.workspace_id,
            "login_username": self.login_username,
            "base_url": self.base_url,
            "user_detail": self.user_detail,
            "all_workspace_ids": self.all_workspace_ids,
            "all_workspace_names": self.all_workspace_names,
            "all_workspace_fair_scheduling": self.all_workspace_fair_scheduling,
            "account": self.account,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WebSession":
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            access_expires_at=data["access_expires_at"],
            refresh_expires_at=data["refresh_expires_at"],
            refresh_offline=bool(data.get("refresh_offline", False))
            or is_offline_refresh_token(data["refresh_token"]),
            workspace_id=data.get("workspace_id"),
            login_username=data.get("login_username"),
            base_url=data.get("base_url"),
            user_detail=data.get("user_detail"),
            all_workspace_ids=data.get("all_workspace_ids"),
            all_workspace_names=data.get("all_workspace_names"),
            all_workspace_fair_scheduling=data.get("all_workspace_fair_scheduling"),
            account=data.get("account"),
            created_at=data["created_at"],
        )

    @blocking_io
    def save(self, account: Optional[str] = None) -> None:
        """Save under the explicit, bound, or current account, in that order."""
        resolved_account = _resolve_account_for_storage(
            account if account is not None else self.account
        )
        if not resolved_account:
            return
        cache_file = get_session_cache_file(resolved_account)
        if cache_file is None:  # pragma: no cover - resolved account is explicit
            return
        self.account = resolved_account
        with exclusive_cache_lock(cache_file):
            cached = WebSession.load(
                allow_expired=True, account=resolved_account, _repair_identity=False
            )
            if cached is not None and cached.created_at > self.created_at:
                return
            atomic_write_text(cache_file, json.dumps(self.to_dict(), ensure_ascii=False), private=True)

    @classmethod
    @blocking_io
    def load(
        cls,
        allow_expired: bool = False,
        account: Optional[str] = None,
        *,
        _repair_identity: bool = True,
    ) -> Optional["WebSession"]:
        """Load the account's cached session, or ``None``.

        Returns ``None`` when no account is active, the file is missing,
        the payload is malformed, or (absent *allow_expired*) the cache
        is past its TTL.
        """
        cache_file = get_session_cache_file(account)
        if cache_file is None or not cache_file.exists():
            return None
        try:
            with open(cache_file, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not _is_valid_session_cache_payload(data):
            return None
        session = cls.from_dict(data)
        session.account = _resolve_account_for_storage(account)
        if _repair_identity and not session.login_username:
            session.login_username = token_login_username(session.access_token)
            if session.login_username:
                session.save(account=account)
        if allow_expired or session.is_valid():
            return session
        return None
