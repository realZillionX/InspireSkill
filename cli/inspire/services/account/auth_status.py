"""Read-only authentication metadata shared by the CLI and SDK.

Remaining lifetimes are integer seconds clamped to zero. Access validity uses
the session layer's 60-second safety margin. No credentials leave this module.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from inspire.platform.web.session.cas import CasSession
from inspire.platform.web.session.models import WebSession, get_session_cache_file


def login_hint(account: str | None, *, cas: bool = False) -> str:
    return (
        "inspire login" + (" --cas" if cas else "") + (f" --account {account}" if account else "")
    )


def legacy_message(account: str | None) -> str:
    return f"Session predates OAuth login (v7.2.0); run {login_hint(account)}."


def _iso(timestamp: float) -> str | None:
    try:
        return datetime.fromtimestamp(timestamp).astimezone().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def inspect_auth_status(account: str | None) -> dict[str, Any]:
    """Inspect local caches without networking, identity repair, or writes."""
    now = time.time()
    path = get_session_cache_file(account)
    session = WebSession.load(allow_expired=True, account=account, _repair_identity=False)
    oauth: dict[str, Any] = {
        "state": "legacy" if path is not None and path.exists() else "missing",
        "access_expires_at": None,
        "refresh_expires_at": None,
        "access_expires_in": None,
        "refresh_expires_in": None,
        "issued_at": None,
        "login_username": None,
    }
    if session is not None:
        oauth.update(
            state=(
                "valid"
                if session.access_expires_at > now + 60
                else "refreshable"
                if session.refresh_expires_at > now
                else "expired"
            ),
            access_expires_at=_iso(session.access_expires_at),
            refresh_expires_at=_iso(session.refresh_expires_at),
            access_expires_in=max(0, int(session.access_expires_at - now)),
            refresh_expires_in=max(0, int(session.refresh_expires_at - now)),
            issued_at=_iso(session.created_at),
            login_username=session.login_username,
        )
    cached_cas = CasSession.load(account=account)
    cas: dict[str, Any] = {
        "state": "missing",
        "method": None,
        "logged_in_at": None,
        "age_seconds": None,
    }
    if cached_cas is not None:
        cas.update(
            state="present",
            method=cached_cas.method,
            logged_in_at=_iso(cached_cas.created_at),
            age_seconds=max(0, int(now - cached_cas.created_at)),
        )
    if oauth["state"] == "expired" and cached_cas is not None:
        oauth["note"] = f"Renewal will be attempted via CAS; otherwise run {login_hint(account)}."
    return {"oauth": oauth, "cas": cas}


def format_duration(seconds: int) -> str:
    if seconds >= 86400:
        return f"{seconds // 86400}d {seconds % 86400 // 3600}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    return f"{seconds // 60}m" if seconds >= 60 else f"{seconds}s"


def format_auth_status(status: dict[str, Any], account: str | None) -> list[str]:
    oauth, cas = status["oauth"], status["cas"]
    lines = []
    for token in ("access", "refresh"):
        expiry = oauth[f"{token}_expires_at"]
        remaining = oauth[f"{token}_expires_in"]
        state = oauth["state"]
        if remaining is not None:
            state = "valid" if remaining > (60 if token == "access" else 0) else "expired/expiring"
        detail = f", expires {expiry} (in {format_duration(remaining)})" if expiry else ""
        lines.append(f"OAuth {token} token: {state}{detail}")
    if oauth["state"] == "refreshable":
        lines.append("OAuth: refreshable; renewed automatically on next use.")
    if oauth.get("note"):
        lines.append(oauth["note"])
    cas_state = cas["state"]
    detail = ""
    if cas_state in {"missing", "expired"}:
        detail = f" — dataset commands need {login_hint(account, cas=True)}"
    lines.append(f"CAS (data plaza): {cas_state}{detail}")
    return lines
