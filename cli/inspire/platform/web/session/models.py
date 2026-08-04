"""Web session models and cache persistence.

Storage: ``~/.inspire/accounts/<active>/web_session.json``, colocated with
the account's ``config.toml`` and ``bridges.json``. Switching account
switches session cache in lockstep.

No legacy fallback: the CLI requires an active account. Session saves
prefer an explicit ``account=`` override, then the account already bound
to the session, and only then ``inspire.accounts.current_account()``.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

SESSION_TTL = 3600  # 1 hour


class SessionExpiredError(Exception):
    """Raised when the web session has expired (401 from server)."""


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


def _is_valid_storage_cookie(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if not isinstance(value.get("name"), str) or not value["name"]:
        return False
    if not isinstance(value.get("value"), str):
        return False

    for field in ("domain", "path"):
        if field in value and not isinstance(value[field], str):
            return False
    if "expires" in value and value["expires"] is not None and not _is_finite_number(
        value["expires"]
    ):
        return False
    for field in ("httpOnly", "secure"):
        if field in value and not isinstance(value[field], bool):
            return False
    if "sameSite" in value and value["sameSite"] not in {"Strict", "Lax", "None"}:
        return False
    return True


def _is_valid_storage_origin(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    origin = value.get("origin")
    if not isinstance(origin, str) or not origin:
        return False
    local_storage = value.get("localStorage", [])
    if not isinstance(local_storage, list):
        return False
    return all(
        isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("value"), str)
        for item in local_storage
    )


def _is_valid_storage_state(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if "cookies" in value:
        cookies = value["cookies"]
        if not isinstance(cookies, list) or not all(
            _is_valid_storage_cookie(cookie) for cookie in cookies
        ):
            return False
    if "origins" in value:
        origins = value["origins"]
        if not isinstance(origins, list) or not all(
            _is_valid_storage_origin(origin) for origin in origins
        ):
            return False
    return True


def _is_valid_session_cache_payload(data: object) -> bool:
    """Return whether a decoded cache has the shape required by its consumers."""
    if not isinstance(data, dict):
        return False
    if not _is_finite_number(data.get("created_at")):
        return False

    storage_state = data.get("storage_state")
    if storage_state is not None and not _is_valid_storage_state(storage_state):
        return False
    if not _is_string_mapping(data.get("cookies")):
        return False

    for field in ("workspace_id", "login_username", "base_url", "account"):
        if data.get(field) is not None and not isinstance(data[field], str):
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
    all_workspace_fair_scheduling: Optional[dict[str, bool]] = None
    account: Optional[str] = None

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
            "all_workspace_fair_scheduling": self.all_workspace_fair_scheduling,
            "account": self.account,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WebSession":
        # Back-compat with older cache files that stored only cookies
        storage_state = data.get("storage_state")
        cookies = data.get("cookies")
        if storage_state is None:
            storage_state = {"cookies": [], "origins": []}
        elif isinstance(storage_state, dict):
            storage_state = dict(storage_state)
            storage_state.setdefault("cookies", [])
            storage_state.setdefault("origins", [])
        else:
            raise ValueError("Invalid session storage state.")
        return cls(
            storage_state=storage_state,
            cookies=cookies,
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
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        # Restrict permissions: session contains sensitive cookies/tokens.
        tmp_path = cache_file.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False)
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
            with open(cache_file, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not _is_valid_session_cache_payload(data):
            return None
        session = cls.from_dict(data)
        session.account = _resolve_account_for_storage(account)
        if allow_expired or session.is_valid():
            return session
        return None
