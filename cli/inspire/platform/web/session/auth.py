"""Authentication helpers for web-session based APIs."""

from __future__ import annotations

from html.parser import HTMLParser
import json
import os
import logging
import re
import time
from http.cookiejar import Cookie
from typing import TYPE_CHECKING, Any, Optional, cast
from urllib.parse import urljoin

from inspire.config import Config

from .models import DEFAULT_WORKSPACE_ID, WebSession
from .browser_launch import (
    chromium_launch_kwargs,
    is_playwright_browser_runtime_error,
    playwright_install_hint,
)
from .proxy import get_playwright_proxy

if TYPE_CHECKING:
    from playwright.sync_api import ProxySettings

logger = logging.getLogger(__name__)

_CAS_PROVIDER_LOGIN_RE = re.compile(r'"loginUrl"\s*:\s*"([^"]*broker[^"]*cas[^"]*login[^"]*)"')
_CAS_RSA_KEY_RE = re.compile(
    r"RSAUtils\.getKeyPair\(\s*['\"]([0-9a-fA-F]+)['\"]\s*,\s*['\"][^'\"]*['\"]\s*,"
    r"\s*['\"]([0-9a-fA-F]+)['\"]"
)


def _load_runtime_config(account: Optional[str] = None) -> Config:
    account_name = str(account or "").strip()
    if account_name:
        return _load_account_runtime_config(account_name)
    config, _ = Config.from_files_and_env(require_credentials=False)
    return config


def _load_account_runtime_config(account: str) -> Config:
    """Load account-level auth/runtime fields without changing active account."""
    from inspire.accounts import account_config_path, account_exists
    from inspire.config.load_common import _default_config_values
    from inspire.config.toml import _flatten_toml, _load_toml, _toml_key_to_field

    if not account_exists(account):
        raise ValueError(f"Account not found: {account}")
    path = account_config_path(account)
    if not path.exists():
        raise ValueError(f"Account config not found: {path}")

    config_dict = _default_config_values()
    raw = _load_toml(path)
    raw.pop("accounts", None)
    raw.pop("context", None)
    for toml_key, value in _flatten_toml(raw).items():
        field_name = _toml_key_to_field(toml_key)
        if field_name and field_name in config_dict:
            config_dict[field_name] = value

    if not config_dict.get("password"):
        env_password = os.getenv("INSPIRE_PASSWORD")
        if env_password:
            config_dict["password"] = env_password

    return Config(**config_dict)


def _session_matches_username(cached: WebSession, username: str) -> bool:
    if not username:
        return True
    if not cached.login_username:
        return False
    return cached.login_username == username


def _has_real_workspace_id(session: WebSession) -> bool:
    value = str(session.workspace_id or "").strip()
    return bool(value) and value != DEFAULT_WORKSPACE_ID


def _is_browser_closed_error(exc: BaseException) -> bool:
    text = str(exc)
    return "Target page, context or browser has been closed" in text


def _is_browser_launch_runtime_error(exc: BaseException) -> bool:
    return is_playwright_browser_runtime_error(exc)


def _raise_browser_launch_runtime_error(exc: BaseException) -> None:
    raise RuntimeError(
        "Playwright Chromium could not start for Inspire login. Prepare the "
        "standard CLI runtime with:\n"
        f"    {playwright_install_hint()}\n"
        "Then retry `inspire init`."
    ) from exc


def _raise_browser_closed_error(exc: BaseException) -> None:
    from inspire.config import ConfigError

    raise ConfigError(
        "Playwright Chromium closed during Inspire login. This is usually a "
        "browser runtime problem in a containerized notebook, not an account "
        "credential problem. InspireSkill launches Chromium with container-safe "
        "sandbox and /dev/shm flags; if this still happens, reinstall the "
        "Playwright browser runtime for the active package and retry."
    ) from exc


_WORKSPACE_ID_PATTERN = re.compile(
    r"^ws-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def _workspace_routes_from_payload(payload: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    workspace_ids: list[str] = []
    workspace_names: dict[str, str] = {}
    for route_group in (payload.get("data") or {}).get("routes") or []:
        if not isinstance(route_group, dict):
            continue
        if route_group.get("name") != "userWorkspaceList":
            continue
        for entry in route_group.get("routes") or []:
            if not isinstance(entry, dict):
                continue
            ws_id = str(entry.get("path") or "").strip()
            ws_name = str(entry.get("name") or "").strip()
            if ws_id and _WORKSPACE_ID_PATTERN.match(ws_id) and ws_id != DEFAULT_WORKSPACE_ID:
                if ws_id not in workspace_names:
                    workspace_ids.append(ws_id)
                    workspace_names[ws_id] = ws_name
    return workspace_ids, workspace_names


def _merge_workspace_routes(
    current_ids: list[str],
    current_names: dict[str, str],
    new_ids: list[str],
    new_names: dict[str, str],
) -> None:
    for ws_id in new_ids:
        if ws_id not in current_names:
            current_ids.append(ws_id)
        if new_names.get(ws_id):
            current_names[ws_id] = new_names[ws_id]


def _cas_rsa_chunk_size(modulus_hex: str) -> int:
    modulus = modulus_hex.lstrip("0") or "0"
    digit_count = (len(modulus) + 3) // 4
    return 2 * (digit_count - 1)


def _legacy_cas_encrypt_password(password: str, exponent_hex: str, modulus_hex: str) -> str:
    """Match the CAS page's legacy RSAUtils.encryptedString implementation."""
    exponent = int(exponent_hex, 16)
    modulus = int(modulus_hex, 16)
    chunk_size = _cas_rsa_chunk_size(modulus_hex)

    values = [ord(ch) for ch in password]
    while len(values) % chunk_size != 0:
        values.append(0)

    encrypted: list[str] = []
    for offset in range(0, len(values), chunk_size):
        block = 0
        for byte_offset, value in enumerate(values[offset : offset + chunk_size]):
            block += value << (8 * byte_offset)
        text = f"{pow(block, exponent, modulus):x}"
        if len(text) % 4:
            text = text.zfill(len(text) + (4 - len(text) % 4))
        encrypted.append(text)
    return " ".join(encrypted)


class _LoginFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self.script_sources: list[str] = []
        self._current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        lower_tag = tag.lower()
        if lower_tag == "form":
            self._current = {"attrs": attrs_dict, "inputs": []}
            self.forms.append(self._current)
            return
        if lower_tag == "input" and self._current is not None:
            self._current["inputs"].append(attrs_dict)
        elif lower_tag == "script" and attrs_dict.get("src"):
            self.script_sources.append(attrs_dict["src"])

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form":
            self._current = None


def _extract_login_form(html: str, page_url: str) -> tuple[str, dict[str, str]]:
    parser = _LoginFormParser()
    parser.feed(html)

    selected: dict[str, Any] | None = None
    for form in parser.forms:
        attrs = form.get("attrs") or {}
        inputs = form.get("inputs") or []
        names = {str(item.get("name") or "") for item in inputs}
        if attrs.get("id") == "fm1" or {"username", "password"}.issubset(names):
            selected = form
            break
    if selected is None:
        raise ValueError("CAS login form not found.")

    attrs = selected.get("attrs") or {}
    action = urljoin(page_url, str(attrs.get("action") or ""))
    fields: dict[str, str] = {}
    for item in selected.get("inputs") or []:
        name = str(item.get("name") or "")
        if not name:
            continue
        fields[name] = str(item.get("value") or "")
    return action, fields


def _extract_script_sources(html: str) -> list[str]:
    parser = _LoginFormParser()
    parser.feed(html)
    return parser.script_sources


def _extract_cas_rsa_key(text: str) -> tuple[str, str] | None:
    match = _CAS_RSA_KEY_RE.search(text)
    if not match:
        return None
    return match.group(1), match.group(2)


def _resolve_cas_rsa_key(http: Any, html: str, page_url: str) -> tuple[str, str]:
    from urllib.parse import urlparse

    key = _extract_cas_rsa_key(html)
    if key:
        return key

    page_host = urlparse(page_url).netloc
    for source in _extract_script_sources(html):
        script_url = urljoin(page_url, source)
        if urlparse(script_url).netloc != page_host:
            continue
        try:
            response = http.get(script_url, timeout=15)
            response.raise_for_status()
        except Exception:
            continue
        key = _extract_cas_rsa_key(response.text)
        if key:
            return key

    raise ValueError("CAS RSA key not found.")


def _decode_keycloak_login_url(html: str, page_url: str) -> str | None:
    match = _CAS_PROVIDER_LOGIN_RE.search(html)
    if not match:
        return None
    raw = match.group(1)
    try:
        decoded = json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        decoded = raw.replace("\\/", "/")
    return urljoin(page_url, decoded)


def _cookie_to_storage_entry(cookie: Cookie) -> dict[str, Any]:
    rest = {str(k).lower(): v for k, v in getattr(cookie, "_rest", {}).items()}
    same_site = str(rest.get("samesite") or "Lax")
    if same_site.lower() not in {"strict", "lax", "none"}:
        same_site = "Lax"
    return {
        "name": cookie.name,
        "value": cookie.value,
        "domain": cookie.domain,
        "path": cookie.path or "/",
        "expires": float(cookie.expires) if cookie.expires else -1,
        "httpOnly": "httponly" in rest,
        "secure": bool(cookie.secure),
        "sameSite": same_site.capitalize(),
    }


def _login_with_cas_requests(
    username: str,
    password: str,
    *,
    base_url: str,
    account: Optional[str] = None,
) -> WebSession:
    import requests
    import urllib3

    from .proxy import resolve_requests_proxy_config

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    proxies, _source = resolve_requests_proxy_config(account=account)
    http = requests.Session()
    http.trust_env = False
    http.proxies.update(proxies)
    http.verify = False
    http.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) InspireSkill",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )

    login_resp = http.get(f"{base_url.rstrip('/')}/login", timeout=30, allow_redirects=True)
    login_resp.raise_for_status()
    cas_login_url = _decode_keycloak_login_url(login_resp.text, login_resp.url)
    if cas_login_url:
        login_resp = http.get(cas_login_url, timeout=30, allow_redirects=True)
        login_resp.raise_for_status()

    action, fields = _extract_login_form(login_resp.text, login_resp.url)
    exponent_hex, modulus_hex = _resolve_cas_rsa_key(http, login_resp.text, login_resp.url)
    fields["username"] = username
    fields["password"] = _legacy_cas_encrypt_password(password, exponent_hex, modulus_hex)
    fields.setdefault("encrypted", "true")
    fields.setdefault("_eventId", "submit")
    fields.setdefault("loginType", "1")

    auth_resp = http.post(
        action,
        data=fields,
        headers={"Referer": login_resp.url},
        timeout=30,
        allow_redirects=True,
    )
    auth_resp.raise_for_status()

    api_headers = {"Accept": "application/json", "Referer": f"{base_url.rstrip('/')}/login"}
    user_detail: dict | None = None
    user_detail_resp = http.get(
        f"{base_url.rstrip('/')}/api/v1/user/detail",
        headers=api_headers,
        timeout=15,
    )
    if user_detail_resp.status_code != 200:
        raise ValueError(
            "Login did not complete. Check that the password is correct and "
            "`auth.username` is the platform login ID (phone, student ID, or email), "
            "not the display name."
        )
    payload = user_detail_resp.json()
    data = payload.get("data")
    if isinstance(data, dict):
        user_detail = data

    all_workspace_ids: list[str] = []
    all_workspace_names: dict[str, str] = {}
    try:
        routes_resp = http.get(
            f"{base_url.rstrip('/')}/api/v1/user/routes/default",
            headers=api_headers,
            timeout=15,
        )
        if routes_resp.status_code == 200:
            route_ids, route_names = _workspace_routes_from_payload(routes_resp.json())
            _merge_workspace_routes(
                all_workspace_ids,
                all_workspace_names,
                route_ids,
                route_names,
            )
    except Exception:
        pass

    storage_state = {
        "cookies": [_cookie_to_storage_entry(cookie) for cookie in http.cookies],
        "origins": [],
    }
    cookie_dict = {cookie.name: cookie.value for cookie in http.cookies}
    workspace_id = all_workspace_ids[0] if all_workspace_ids else DEFAULT_WORKSPACE_ID
    session = WebSession(
        storage_state=storage_state,
        cookies=cookie_dict,
        workspace_id=workspace_id,
        login_username=username,
        base_url=base_url,
        user_detail=user_detail,
        all_workspace_ids=all_workspace_ids or None,
        all_workspace_names=all_workspace_names or None,
        created_at=time.time(),
    )
    session.save(account=account)
    return session


def get_credentials(account: Optional[str] = None) -> tuple[str, str]:
    """Get web credentials from layered config (project/global/env/default)."""
    config = _load_runtime_config(account)
    username = (config.username or "").strip()
    password = config.password or ""

    if not username or not password:
        raise ValueError(
            "Missing web authentication credentials. Run 'inspire account add <name>' "
            "to create an account, or set INSPIRE_PASSWORD when the account's "
            "config.toml lacks [auth].password."
        )

    return username, password


def login_with_playwright(
    username: str,
    password: str,
    base_url: str = "https://api.example.com",
    headless: bool = True,
    account: Optional[str] = None,
) -> WebSession:
    """Login to Inspire web UI using Playwright and capture session storage state.

    The login flow: qz/login -> CAS (Keycloak broker) -> Keycloak -> qz.
    """
    from inspire.platform.web.browser_api.core import _in_asyncio_loop, _run_in_thread

    if _in_asyncio_loop():
        return _run_in_thread(
            login_with_playwright,
            username,
            password,
            base_url=base_url,
            headless=headless,
            account=account,
        )

    from playwright.sync_api import sync_playwright

    try:
        return _login_with_cas_requests(
            username,
            password,
            base_url=base_url,
            account=account,
        )
    except Exception:
        logger.debug("CAS requests login failed; falling back to Playwright.", exc_info=True)

    proxy = cast("ProxySettings | None", get_playwright_proxy(account=account))
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(**chromium_launch_kwargs(headless=headless, proxy=proxy))
        except Exception as exc:
            if _is_browser_launch_runtime_error(exc):
                _raise_browser_launch_runtime_error(exc)
            raise
        context = browser.new_context(proxy=proxy, ignore_https_errors=True)
        page = context.new_page()

        # Navigate to login page; use domcontentloaded since CAS may have
        # long-polling resources that prevent networkidle from completing.
        try:
            page.goto(f"{base_url}/login", wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            if _is_browser_closed_error(exc):
                _raise_browser_closed_error(exc)
            raise
        # Give some time for any redirects to settle
        page.wait_for_timeout(2000)

        login_pairs = [
            ("input#username", "input#passwordShow"),
            ("input[name='username']", "input[name='password']"),
            ("input[placeholder='Username/alias']", "input[placeholder='Password']"),
        ]

        def _fill_login_form() -> Optional[object]:
            for user_sel, pass_sel in login_pairs:
                try:
                    page.wait_for_selector(user_sel, timeout=5000, state="visible")
                    page.wait_for_selector(pass_sel, timeout=5000, state="visible")
                    user_locator = page.locator(user_sel).first
                    pass_locator = page.locator(pass_sel).first
                    user_locator.fill(username)
                    pass_locator.fill(password)
                    return pass_locator
                except Exception:
                    continue
            return None

        def _submit_login_form(pass_locator) -> None:  # noqa: ANN001
            try:
                pass_locator.press("Enter", timeout=3000)
                return
            except Exception:
                pass
            try:
                pass_locator.evaluate("el => el.form && el.form.submit()")
                return
            except Exception:
                pass
            try:
                pass_locator.evaluate(
                    """
                    el => {
                      const btn = el.form?.querySelector('#passbutton,button[type="submit"],input[type="submit"]');
                      if (btn) { btn.click(); return true; }
                      return false;
                    }
                    """
                )
            except Exception:
                pass

        pass_locator = _fill_login_form()
        if not pass_locator:
            try:
                page.get_by_text("Account login", exact=True).click(timeout=3000, force=True)
                page.wait_for_timeout(500)
            except Exception:
                pass
            pass_locator = _fill_login_form()

        if pass_locator:
            _submit_login_form(pass_locator)

        def _wait_for_api_auth() -> None:
            deadline = time.time() + 30
            headers = {
                "Accept": "application/json",
                "Referer": f"{base_url}/login",
            }
            while time.time() < deadline:
                try:
                    resp = context.request.get(
                        f"{base_url}/api/v1/user/detail",
                        headers=headers,
                        timeout=10000,
                    )
                    if resp.status == 200:
                        return
                except Exception:
                    pass
                page.wait_for_timeout(500)
            raise ValueError(
                "Login did not complete. Check that the password is correct and "
                "`auth.username` is the platform login ID (phone, student ID, or email), "
                "not the display name."
            )

        _wait_for_api_auth()
        # Once authenticated cookies are available, stop the page quickly and
        # use request APIs for discovery.  Some minimal GPU notebook images can
        # start Chromium but crash while rendering the full Qizhi SPA because
        # fontconfig is incomplete; rendering the SPA is unnecessary for CLI
        # session capture.
        try:
            page.close()
        except Exception:
            pass

        user_detail: dict | None = None
        request_headers = {
            "Accept": "application/json",
            "Referer": f"{base_url}/login",
        }
        try:
            user_detail_resp = context.request.get(
                f"{base_url}/api/v1/user/detail",
                headers=request_headers,
                timeout=10000,
            )
            if user_detail_resp.status == 200:
                payload = user_detail_resp.json()
                data = payload.get("data")
                if isinstance(data, dict):
                    user_detail = data
        except Exception:
            user_detail = None

        # Discover all workspace IDs via /api/v1/user/routes/default.
        # The response contains a "userWorkspaceList" route with all workspaces
        # the user can access, each with name (display name) and path (ws-... ID).
        all_workspace_ids: list[str] = []
        all_workspace_names: dict[str, str] = {}
        for routes_workspace_id in ("default",):
            try:
                routes_resp = context.request.get(
                    f"{base_url}/api/v1/user/routes/{routes_workspace_id}",
                    headers=request_headers,
                    timeout=15000,
                )
                if routes_resp.status == 200:
                    route_ids, route_names = _workspace_routes_from_payload(routes_resp.json())
                    _merge_workspace_routes(
                        all_workspace_ids,
                        all_workspace_names,
                        route_ids,
                        route_names,
                    )
            except Exception:
                pass

        workspace_id = all_workspace_ids[0] if all_workspace_ids else DEFAULT_WORKSPACE_ID

        # Capture storage state (cookies + localStorage)
        storage_state = context.storage_state()

        # Keep a simple cookie name->value mapping for debugging/back-compat
        cookies = context.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies}

        browser.close()

        session = WebSession(
            storage_state=cast(dict[str, Any], storage_state),
            cookies=cookie_dict,
            workspace_id=workspace_id,
            login_username=username,
            base_url=base_url,
            user_detail=user_detail,
            all_workspace_ids=all_workspace_ids or None,
            all_workspace_names=all_workspace_names or None,
            account=account,
            created_at=time.time(),
        )
        session.save(account=account)

        return session


def get_web_session(
    force_refresh: bool = False,
    require_workspace: bool = False,
    account: Optional[str] = None,
) -> WebSession:
    """Get a valid web session, logging in if necessary.

    Args:
        force_refresh: Force a new login even if cached session exists.
        require_workspace: Force re-login if workspace_id is missing.

    Returns:
        A valid WebSession with storage_state and optionally workspace_id.
    """
    # Resolve credentials early so we can avoid reusing a cache from another user.
    credentials_error: Optional[ValueError] = None
    try:
        username, password = get_credentials(account)
    except ValueError as e:
        credentials_error = e
        try:
            username = (_load_runtime_config(account).username or "").strip()
        except Exception:
            username = ""
        password = ""

    if not force_refresh:
        cached = WebSession.load(account=account)
        if cached and cached.storage_state.get("cookies"):
            if require_workspace and not _has_real_workspace_id(cached):
                pass
            elif username and not _session_matches_username(cached, username):
                # Credentials are available and don't match the cached login user.
                # Force fresh login so the active account follows current config.
                pass
            else:
                return cached

    # If we can't refresh (missing credentials), try the cached session anyway.
    if credentials_error is not None:
        cached = WebSession.load(allow_expired=True, account=account)
        if cached and cached.storage_state.get("cookies"):
            if require_workspace and not _has_real_workspace_id(cached):
                raise credentials_error
            return cached
        raise credentials_error

    # Use cached session if available and has cookies, even if beyond TTL.
    # The session cookies may still be valid server-side; let API calls determine validity.
    # Skip this when force_refresh is set — the caller explicitly wants a fresh login.
    if not force_refresh:
        cached = WebSession.load(allow_expired=True, account=account)
        if cached and cached.storage_state.get("cookies"):
            if (
                not require_workspace or _has_real_workspace_id(cached)
            ) and _session_matches_username(cached, username):
                # Use cached session; server will reject if truly invalid.
                return cached

    # Session is missing or has no cookies, perform fresh login
    base_url = _load_runtime_config(account).base_url
    return login_with_playwright(username, password, base_url=base_url, account=account)
