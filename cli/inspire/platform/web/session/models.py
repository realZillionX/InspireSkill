"""Web session models and cache persistence.

Storage: ``~/.inspire/accounts/<active>/web_session.json``, colocated with
the account's ``config.toml`` and ``bridges.json``. Switching account
switches session cache in lockstep.

No legacy fallback: the CLI requires an active account, and the session
cache keys off whatever ``inspire.accounts.current_account()`` returns
at call time. An explicit ``account=`` override is still accepted for
the rare callsite that knows better.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

SESSION_TTL = 3600  # 1 hour


class SessionExpiredError(Exception):
    """Raised when the web session has expired (401 from server)."""


# Default workspace placeholder (override with INSPIRE_WORKSPACE_ID env var)
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


@dataclass
class WebSession:
    """Captured web session for web-ui APIs.

    We store Playwright `storage_state` because the web-ui APIs behind `/api/v1/*`
    are protected by Keycloak/CAS SSO and can require more than just a couple
    of cookies.
    """

    storage_state: dict[str, Any]
    created_at: float
    workspace_id: Optional[str] = None
    login_username: Optional[str] = None
    base_url: Optional[str] = None
    user_detail: Optional[dict[str, Any]] = None
    all_workspace_ids: Optional[list[str]] = None
    all_workspace_names: Optional[dict[str, str]] = None

    # Back-compat: older cache stored only name->value cookies
    cookies: Optional[dict[str, str]] = None

    def is_valid(self) -> bool:
        """Check if session is still valid (not expired)."""
        return (time.time() - self.created_at) < SESSION_TTL

    def to_dict(self) -> dict:
        return {
            "storage_state": self.storage_state,
            "cookies": self.cookies,
            "workspace_id": self.workspace_id,
            "login_username": self.login_username,
            "base_url": self.base_url,
            "user_detail": self.user_detail,
            "all_workspace_ids": self.all_workspace_ids,
            "all_workspace_names": self.all_workspace_names,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WebSession":
        # Back-compat with older cache files that stored only cookies
        storage_state = data.get("storage_state")
        cookies = data.get("cookies")
        if storage_state is None:
            storage_state = {"cookies": [], "origins": []}
        return cls(
            storage_state=storage_state,
            cookies=cookies,
            workspace_id=data.get("workspace_id"),
            login_username=data.get("login_username"),
            base_url=data.get("base_url"),
            user_detail=data.get("user_detail"),
            all_workspace_ids=data.get("all_workspace_ids"),
            all_workspace_names=data.get("all_workspace_names"),
            created_at=data["created_at"],
        )

    def save(self, account: Optional[str] = None) -> None:
        """Save session under the account's directory. No-op without an account."""
        cache_file = get_session_cache_file(account)
        if cache_file is None:
            return
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        # Restrict permissions: session contains sensitive cookies/tokens.
        tmp_path = cache_file.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(self.to_dict(), f)
        os.replace(tmp_path, cache_file)
        try:
            os.chmod(cache_file, 0o600)
        except Exception:
            pass

    @classmethod
    def load(
        cls,
        allow_expired: bool = False,
        account: Optional[str] = None,
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
            with open(cache_file) as f:
                data = json.load(f)
            session = cls.from_dict(data)
        except (json.JSONDecodeError, KeyError):
            return None
        if allow_expired or session.is_valid():
            return session
        return None
