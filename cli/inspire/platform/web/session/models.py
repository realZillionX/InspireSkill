"""Web session models and cache persistence.

Primary storage: ``~/.inspire/accounts/<active>/web_session.json`` — colocated
with the account's config.toml and bridges.json, so switching accounts
switches session cache in lockstep. A legacy unscoped path
``~/.cache/inspire-skill/web_session.json`` remains usable when no account
is active (old installs without ``inspire account`` never migrated).

Legacy read fallbacks (to be retired when ``inspire account migrate`` lands):
previous releases wrote per-user cache at
``~/.cache/inspire-skill/web_session-<normalized-user>.json``. ``load()``
still reads those files once so an upgrade doesn't force a fresh login.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# Legacy cache location — still used when no InspireSkill account is active,
# and as a read-only fallback for data written by older releases.
SESSION_CACHE_DIR = Path.home() / ".cache" / "inspire-skill"
SESSION_CACHE_FILE = SESSION_CACHE_DIR / "web_session.json"
SESSION_TTL = 3600  # 1 hour


class SessionExpiredError(Exception):
    """Raised when the web session has expired (401 from server)."""


# Default workspace placeholder (override with INSPIRE_WORKSPACE_ID env var)
DEFAULT_WORKSPACE_ID = "ws-00000000-0000-0000-0000-000000000000"


def normalize_account_for_cache(account: Optional[str]) -> Optional[str]:
    """Sanitize an account string for use in legacy cache filenames.

    Kept for legacy fallback only — new storage puts sessions inside
    ``~/.inspire/accounts/<name>/`` where names are already validated.
    """
    if not account:
        return None
    value = account.strip()
    if not value:
        return None
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    return normalized or None


def _resolve_account_for_storage(explicit: Optional[str]) -> Optional[str]:
    """Pick the account whose directory holds the session cache.

    Order (mirrors the tunnel config resolver — no env-var chain):
      1. *explicit* parameter
      2. ``~/.inspire/current`` via :mod:`inspire.accounts`
      3. ``None`` — sessions live at the legacy unscoped path
    """
    candidate = (explicit or "").strip()
    if candidate:
        return candidate
    try:
        from inspire.accounts import current_account
    except ImportError:  # pragma: no cover - accounts ships with the CLI
        return None
    return current_account()


def _account_session_path(name: str) -> Path:
    return Path.home() / ".inspire" / "accounts" / name / "web_session.json"


def _legacy_scoped_session_path(name: str) -> Path:
    normalized = normalize_account_for_cache(name)
    if not normalized:
        return SESSION_CACHE_FILE
    return SESSION_CACHE_DIR / f"web_session-{normalized}.json"


def get_session_cache_file(account: Optional[str] = None) -> Path:
    """Resolve the on-disk path for the session cache.

    Writes always target this path. Reads should use
    :meth:`WebSession.load`, which additionally consults legacy fallbacks.
    """
    name = _resolve_account_for_storage(account)
    if name:
        return _account_session_path(name)
    return SESSION_CACHE_FILE


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
        """Save session to the account-scoped cache file.

        When *account* is omitted, falls back to ``inspire.accounts.current_account()``
        and finally to the legacy unscoped location.
        """
        cache_file = get_session_cache_file(account)
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
        """Load session from cache. Primary path plus one-shot legacy fallbacks.

        Resolution order:
          1. ``~/.inspire/accounts/<account>/web_session.json`` (primary)
          2. ``~/.cache/inspire-skill/web_session-<account>.json`` (legacy per-user)
          3. ``~/.cache/inspire-skill/web_session.json`` (legacy unscoped)

        Falls through on parse error or expired session (unless *allow_expired*).
        """
        name = _resolve_account_for_storage(account)

        candidates: list[Path] = []
        if name:
            candidates.append(_account_session_path(name))
            legacy_per_user = _legacy_scoped_session_path(name)
            if legacy_per_user != SESSION_CACHE_FILE:
                candidates.append(legacy_per_user)
        if SESSION_CACHE_FILE not in candidates:
            candidates.append(SESSION_CACHE_FILE)

        for cache_file in candidates:
            if not cache_file.exists():
                continue
            try:
                with open(cache_file) as f:
                    data = json.load(f)
                session = cls.from_dict(data)
                if allow_expired or session.is_valid():
                    return session
            except (json.JSONDecodeError, KeyError):
                continue
        return None
