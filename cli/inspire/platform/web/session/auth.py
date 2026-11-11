"""Explicit device login and serialized rotating-token refresh."""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Optional

import requests

from inspire.config import Config
from inspire.platform.web.flow import (
    Program,
    blocking_io,
    call,
    enter_context,
    exit_context,
    workflow,
)

from . import models
from .cas import CasSession, approve_device, cas_ticket_valid
from .envelope import _v2_result
from .identity import token_login_username
from .models import AuthenticationError, WebSession
from .oauth import DeviceAuthorization
from .proxy import resolve_requests_proxy_config
from .refresh_lock import exclusive_session_refresh
from .requests import build_requests_session
from .workspaces import ACCESSIBLE_WORKSPACES_PATH, parse_workspace_catalog

logger = logging.getLogger(__name__)
USER_DETAIL_PATH = "/api/v2/user?Action=GetUserDetail"


@blocking_io
def _load_runtime_config(account: Optional[str] = None) -> Config:
    """Use the same account and runtime settings for login and API requests."""
    # Accounts imports browser runtime helpers while initializing this package.
    from inspire.accounts import account_exists

    account_name = str(account or "").strip()
    if account_name:
        if not account_exists(account_name):
            raise ValueError(f"Account not found: {account_name}")
        config, _ = Config.from_files_and_env(require_credentials=False, account=account_name)
    else:
        config, _ = Config.from_files_and_env(require_credentials=False)
    return config


def login_required(account: str | None) -> AuthenticationError:
    """Build an actionable error for an account requiring device authorization."""
    from inspire.accounts import default_account

    suffix = f" --account {account}" if account and account != default_account() else ""
    return AuthenticationError(f"Not logged in; run inspire login{suffix}.")


def _http(account: str | None) -> requests.Session:
    http = requests.Session()
    proxies, source = resolve_requests_proxy_config(account=account)
    if source in {"explicit_env", "toml"} and proxies:
        http.proxies.update(proxies)
        http.trust_env = False
    return http


def _save_existing_account(session: WebSession, account: str | None) -> None:
    """Persist after network I/O; caller must hold exclusive_session_refresh."""
    from inspire.accounts import account_exists

    name = models._resolve_account_for_storage(account)
    if not name or not account_exists(name):
        raise AuthenticationError(
            f"Account {name} was removed; run inspire login --account {name}."
        )
    session.save(account=name)


@blocking_io
def refresh_cached(account: str | None, observed: float | None = None) -> WebSession:
    """Caller holds the refresh lock; always reread the rotating token."""
    cached = WebSession.load(allow_expired=True, account=account)
    if cached is None:
        raise login_required(account)
    if cached.is_valid() and (observed is None or cached.created_at > observed):
        return cached
    if cached.refresh_expires_at <= time.time():
        return _renew_with_cas(cached, account)
    try:
        with _http(account) as http:
            tokens = DeviceAuthorization(
                cached.base_url or _load_runtime_config(account).base_url, http
            ).refresh(cached.refresh_token)
    except AuthenticationError:
        return _renew_with_cas(cached, account)
    refreshed = replace(
        cached,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        access_expires_at=tokens.access_expires_at,
        refresh_expires_at=tokens.refresh_expires_at,
        created_at=time.time(),
        login_username=cached.login_username or token_login_username(tokens.access_token),
    )
    _save_existing_account(refreshed, account)
    return refreshed


def _renew_with_cas(cached: WebSession, account: str | None) -> WebSession:
    """Caller owns the refresh lock; silent renewal never prompts or relocks."""
    cas = CasSession.load(account)
    if cas is None or not cas_ticket_valid(cas, account):
        raise login_required(account)
    base_url = cached.base_url or _load_runtime_config(account).base_url
    with cas.http(account) as cas_http, _http(account) as http:

        def notify(uri: str, code: str, expires: float) -> None:
            if not approve_device(cas_http, uri, base_url):
                raise login_required(account)

        try:
            tokens = DeviceAuthorization(base_url, http).login(notify)
        except (AuthenticationError, requests.RequestException):
            raise login_required(account) from None
    renewed = replace(
        cached,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        access_expires_at=tokens.access_expires_at,
        refresh_expires_at=tokens.refresh_expires_at,
        created_at=time.time(),
    )
    _save_existing_account(renewed, account)
    return renewed


@workflow
def get_web_session(
    force_refresh: bool = False,
    require_workspace: bool = False,
    account: Optional[str] = None,
    lock_timeout: float | None = None,
) -> Program[WebSession]:
    """Load or renew cached tokens without prompting or opening a browser."""
    account = models._resolve_account_for_storage(account)
    cached = yield call(WebSession.load, allow_expired=True, account=account)
    if cached is None:
        raise login_required(account)
    if force_refresh or not cached.is_valid():
        context = exclusive_session_refresh(account, timeout=lock_timeout)
        yield call(enter_context, context)
        try:
            observed = cached.created_at if force_refresh else None
            cached = yield call(refresh_cached, account, observed)
        finally:
            yield call(exit_context, context, *sys.exc_info())
    if require_workspace and not _has_workspace(cached):
        cached = yield call(_ensure_workspace_catalog, cached, account)
    return cached


def _has_workspace(session: WebSession) -> bool:
    value = str(session.workspace_id or "").strip()
    return bool(value) and value != models.DEFAULT_WORKSPACE_ID


@blocking_io
def _ensure_workspace_catalog(session: WebSession, account: str | None) -> WebSession:
    """Discover the workspace catalog a session was saved without, and keep it."""
    base_url = session.base_url or _load_runtime_config(account).base_url
    _discover_identity(session, base_url)
    if _has_workspace(session):
        with exclusive_session_refresh(account):
            _save_existing_account(session, account)
    return session


@workflow
def refresh_session(
    session: WebSession, *, lock_timeout: float | None = None
) -> Program[WebSession]:
    """Refresh a refused generation under the account lock."""
    context = exclusive_session_refresh(session.account, timeout=lock_timeout)
    yield call(enter_context, context)
    try:
        return (yield call(refresh_cached, session.account, session.created_at))
    finally:
        yield call(exit_context, context, *sys.exc_info())


def _discover_identity(session: WebSession, base_url: str) -> None:
    """Fill user detail and the workspace catalog; failures leave the tokens usable."""
    try:
        with build_requests_session(session, base_url) as http:
            try:
                detail = http.post(
                    base_url.rstrip("/") + USER_DETAIL_PATH,
                    json={},
                    timeout=30,
                    allow_redirects=False,
                )
                detail.raise_for_status()
                session.user_detail = _v2_result(detail.json())
                extra_info = session.user_detail.get("extra_info")
                if not session.login_username and isinstance(extra_info, dict):
                    login_name = extra_info.get("login_name")
                    if isinstance(login_name, str) and login_name.strip():
                        session.login_username = login_name.strip()
            except Exception:
                logger.warning("User detail discovery failed; tokens remain usable.")
            try:
                workspaces = http.post(
                    base_url.rstrip("/") + ACCESSIBLE_WORKSPACES_PATH,
                    json={},
                    timeout=30,
                    allow_redirects=False,
                )
                workspaces.raise_for_status()
                (
                    session.all_workspace_ids,
                    session.all_workspace_names,
                    session.all_workspace_fair_scheduling,
                ) = parse_workspace_catalog(workspaces.json())
                session.workspace_id = next(iter(session.all_workspace_ids or []), None)
            except Exception:
                logger.warning("Workspace discovery failed; tokens remain usable.")
    except Exception:
        logger.warning("Session discovery could not start; tokens remain usable.")


@blocking_io
def device_login(account: str, notify: Callable[[str, str, float], None]) -> WebSession:
    """Authorize without holding the refresh lock, then enrich saved tokens."""
    config = _load_runtime_config(account)
    with _http(account) as http:
        tokens = DeviceAuthorization(config.base_url, http).login(notify)
    session = WebSession(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        access_expires_at=tokens.access_expires_at,
        refresh_expires_at=tokens.refresh_expires_at,
        created_at=time.time(),
        account=account,
        base_url=config.base_url,
        login_username=token_login_username(tokens.access_token),
    )
    with exclusive_session_refresh(account):
        session.created_at = time.time()
        _save_existing_account(session, account)
    _discover_identity(session, config.base_url)
    with exclusive_session_refresh(account):
        _save_existing_account(session, account)
    return session
