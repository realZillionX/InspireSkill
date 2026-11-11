"""CAS credentials and SMS login, isolated from the platform Bearer session."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import requests

from inspire.accounts.cache_lock import exclusive_cache_lock
from inspire.local_files import atomic_write_text
from inspire.platform.web.flow import blocking_io
from .models import AuthenticationError, _is_finite_number, get_session_cache_file
from .proxy import resolve_requests_proxy_config

CAS_HOST = "cas.sii.edu.cn"
CAS_BASE_URL = f"https://{CAS_HOST}"
SERVICE = "https://aip.sii.edu.cn/"
LOGIN_URL = CAS_BASE_URL + "/cas/login?service=" + quote(SERVICE, safe="")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


class CasError(AuthenticationError):
    """A sanitized CAS authentication failure."""


class CasTicketExpired(CasError):
    """CAS requires a new interactive login."""


class SmsCodeRejected(CasError):
    """The submitted SMS code was not accepted."""


def cache_path(account: str | None, filename: str) -> Path:
    path = get_session_cache_file(account)
    if path is None:
        raise CasError("Select an account before CAS login.")
    return path.with_name(filename)


def _cookies(http: requests.Session) -> list[dict[str, Any]]:
    return [
        dict(
            name=c.name,
            value=c.value,
            domain=c.domain,
            path=c.path,
            expires=c.expires,
            secure=c.secure,
        )
        for c in http.cookies
        if c.domain.lstrip(".") == CAS_HOST
    ]


def _valid_cookies(value: object) -> bool:
    return isinstance(value, list) and all(
        isinstance(c, dict)
        and set(c) == {"name", "value", "domain", "path", "expires", "secure"}
        and all(isinstance(c.get(k), str) for k in ("name", "value", "domain", "path"))
        and c["domain"].lstrip(".") == CAS_HOST
        and c["path"].startswith("/")
        and isinstance(c.get("secure"), bool)
        and (c.get("expires") is None or _is_finite_number(c["expires"]))
        for c in value
    )


def build_cas_http(cookies: list[dict[str, Any]], account: str | None) -> requests.Session:
    if not _valid_cookies(cookies):
        raise CasError("Invalid CAS cookie cache.")
    http = requests.Session()
    http.headers["User-Agent"] = USER_AGENT
    proxies, source = resolve_requests_proxy_config(account=account)
    if source in {"explicit_env", "toml"} and proxies:
        http.proxies.update(proxies)
        http.trust_env = False
    for c in cookies:
        http.cookies.set_cookie(requests.cookies.create_cookie(**c))
    return http


@dataclass
class CasSession:
    cookies: list[dict[str, Any]] = field(repr=False)
    created_at: float
    method: str

    def http(self, account: str | None = None) -> requests.Session:
        return build_cas_http(self.cookies, account)

    @blocking_io
    def save(self, account: str | None = None) -> None:
        path = cache_path(account, "cas_session.json")
        with exclusive_cache_lock(path):
            atomic_write_text(path, json.dumps(asdict(self)), private=True)

    @classmethod
    @blocking_io
    def load(cls, account: str | None = None) -> CasSession | None:
        try:
            path = cache_path(account, "cas_session.json")
            data = json.loads(path.read_text())
            if (
                not isinstance(data, dict)
                or not _valid_cookies(data.get("cookies"))
                or not _is_finite_number(data.get("created_at"))
                or data.get("method") not in ("sms", "feishu")
                or not any(c["name"] == "CASTGC" and c["value"] for c in data["cookies"])
            ):
                return None
            return cls(data["cookies"], data["created_at"], data["method"])
        except (OSError, ValueError, TypeError):
            return None


def ticket_from_response(response: requests.Response) -> str:
    target = urlsplit(response.headers.get("Location", ""))
    if (
        response.status_code not in (302, 303)
        or target.scheme != "https"
        or target.netloc != "aip.sii.edu.cn"
    ):
        return ""
    return parse_qs(target.query).get("ticket", [""])[0]


def request_timeout(limit: float = 30) -> float:
    from inspire.platform.web.runtime import active_transport

    owner = active_transport.get()
    return min(limit, owner.remaining()) if owner is not None else limit


def mint_ticket(http: requests.Session, timeout: float = 30) -> str:
    try:
        response = http.get(LOGIN_URL, allow_redirects=False, timeout=request_timeout(timeout))
        ticket = ticket_from_response(response)
    except requests.RequestException:
        raise CasError("CAS could not be reached.") from None
    if not ticket:
        raise CasTicketExpired("CAS login expired; run inspire login --cas.")
    return ticket


def cas_ticket_valid(session: CasSession, account: str | None = None) -> bool:
    try:
        with session.http(account) as http:
            mint_ticket(http)
        return True
    except CasError:
        return False


class Forms(HTMLParser):
    """Read form controls without executing page scripts."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[tuple[dict[str, str], dict[str, str]]] = []
        self.current: tuple[dict[str, str], dict[str, str]] | None = None
        self.feed(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: v or "" for k, v in attrs}
        if tag == "form":
            self.current = (values, {})
            self.forms.append(self.current)
        elif tag in ("input", "button") and self.current and values.get("name"):
            self.current[1][values["name"]] = values.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.current = None


def validate_phone(phone: str) -> str:
    if not re.fullmatch(r"(?:\+86)?1\d{10}", phone, flags=re.ASCII):
        raise CasError(
            "A mobile phone number is required: 11 digits starting with 1, optionally +86."
        )
    return phone.removeprefix("+86")


@dataclass
class SmsPending:
    cookies: list[dict[str, Any]] = field(repr=False)
    fields: dict[str, str] = field(repr=False)
    created_at: float

    @blocking_io
    def save(self, account: str) -> None:
        path = cache_path(account, "cas_pending.json")
        # Never serialize username, code, or arbitrary server controls.
        data = asdict(self)
        data["fields"] = {
            k: v for k, v in self.fields.items() if k in ("execution", "_eventId", "loginType")
        }
        with exclusive_cache_lock(path):
            atomic_write_text(path, json.dumps(data), private=True)

    @classmethod
    @blocking_io
    def load(cls, account: str) -> SmsPending:
        try:
            data = json.loads(cache_path(account, "cas_pending.json").read_text())
            if (
                not isinstance(data, dict)
                or not _valid_cookies(data.get("cookies"))
                or not _is_finite_number(data.get("created_at"))
                or not 0 <= time.time() - data["created_at"] < 600
            ):
                raise ValueError
            fields = data.get("fields")
            if (
                not isinstance(fields, dict)
                or not isinstance(fields.get("execution"), str)
                or not fields["execution"]
                or not all(
                    k in ("execution", "_eventId", "loginType") and isinstance(v, str)
                    for k, v in fields.items()
                )
            ):
                raise ValueError
            return cls(data["cookies"], fields, data["created_at"])
        except (OSError, ValueError, KeyError, TypeError):
            raise CasError(
                "SMS pending state missing or expired; send a new code with inspire login --cas sms --phone NUMBER."
            ) from None


@blocking_io
def sms_send_code(phone: str, http: requests.Session) -> SmsPending:
    phone = validate_phone(phone)
    try:
        page = http.get(LOGIN_URL, timeout=30, allow_redirects=False)
        fields = next((f for a, f in Forms(page.text).forms if a.get("id") == "fm2"), None)
        if page.status_code != 200 or not fields or not fields.get("execution"):
            raise CasError("CAS SMS login form is unavailable.")
        response = http.post(
            CAS_BASE_URL + "/cas/smsLogin/sendSms",
            data={"request_username": phone, "isEn": "false"},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": LOGIN_URL},
            timeout=30,
            allow_redirects=False,
        )
        if response.status_code != 200 or response.json().get("success") is not True:
            raise CasError("CAS could not send an SMS code; try again later.")
        return SmsPending(
            _cookies(http),
            {k: v for k, v in fields.items() if k in ("execution", "_eventId", "loginType")},
            time.time(),
        )
    except (requests.RequestException, ValueError, AttributeError) as error:
        if isinstance(error, CasError):
            raise
        raise CasError("CAS SMS request failed.") from None


@blocking_io
def sms_complete(phone: str, code: str, pending: SmsPending, http: requests.Session) -> CasSession:
    phone = validate_phone(phone)
    for cookie in pending.cookies:
        http.cookies.set_cookie(requests.cookies.create_cookie(**cookie))
    if not 0 <= time.time() - pending.created_at < 600:
        raise CasError("SMS pending state expired; send a new code.")
    fields = {
        k: v for k, v in pending.fields.items() if k in ("execution", "_eventId", "loginType")
    }
    fields.update(username=phone, smscode=code, _eventId="submit", loginType="2")
    try:
        response = http.post(
            LOGIN_URL,
            data=fields,
            headers={"Referer": LOGIN_URL},
            allow_redirects=False,
            timeout=30,
        )
    except requests.RequestException:
        raise CasError("CAS SMS login could not be completed.") from None
    if response.status_code == 200:
        # A rejected form may rotate its execution identifier.
        for attrs, fresh in Forms(response.text).forms:
            if attrs.get("id") == "fm2" and fresh.get("execution"):
                pending.fields["execution"] = fresh["execution"]
        pending.cookies = _cookies(http)
        raise SmsCodeRejected("SMS code was not accepted; check the code or send a new one.")
    cookies = _cookies(http)
    if not ticket_from_response(response) or not any(
        c["name"] == "CASTGC" and c["value"] for c in cookies
    ):
        raise CasError("CAS login did not return a valid session.")
    return CasSession(cookies, time.time(), "sms")


def approve_device(cas_http: requests.Session, verification_uri: str, base_url: str) -> bool:
    from .cas_device import approve_device as approve

    return approve(cas_http, verification_uri, base_url)


def feishu_qr_login(
    show_qr: Callable[[Path], None], account: str, timeout: float = 300
) -> CasSession:
    from .cas_qr import feishu_qr_login as login

    return login(show_qr, account, timeout)
