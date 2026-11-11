"""Bounded, origin-checked CAS approval of an OAuth device request."""

from __future__ import annotations

import html
import json
import re
from urllib.parse import urljoin, urlsplit

import requests

from .cas import CAS_HOST, Forms, request_timeout


def approve_device(cas_http: requests.Session, verification_uri: str, base_url: str) -> bool:
    """Return false on unsupported pages; never follow an unchecked redirect.

    The verification URI comes from the configured platform's device endpoint
    and identifies its Keycloak origin. Page contents cannot extend this set.
    """
    try:
        base, verification = urlsplit(base_url), urlsplit(verification_uri)
        if base.scheme != "https" or verification.scheme != "https":
            return False
        allowed = {base.hostname, verification.hostname, CAS_HOST}

        def safe(url: str) -> bool:
            parts = urlsplit(url)
            return (
                parts.scheme == "https"
                and parts.hostname in allowed
                and parts.port in (None, 443)
                and not parts.username
                and not parts.password
            )

        url, method, data = verification_uri, "GET", None
        for _ in range(20):
            if not safe(url):
                return False
            page = cas_http.request(
                method, url, data=data, timeout=request_timeout(), allow_redirects=False
            )
            if page.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, page.headers.get("Location", ""))
                if page.status_code in (301, 302, 303):
                    method, data = "GET", None
                continue
            if page.status_code != 200:
                return False
            if urlsplit(url).path.rstrip("/").endswith("/device/status"):
                return True
            text = html.unescape(page.text).replace(r"\/", "/")
            grant = re.search(r'"oauthAction"\s*:\s*("(?:\\.|[^"\\])*")', text)
            code = re.search(r'"oauth"\s*:\s*\{[^}]*?"code"\s*:\s*("(?:\\.|[^"\\])*")', text, re.S)
            if grant and code:
                url = urljoin(url, json.loads(grant[1]))
                method, data = "POST", {"code": json.loads(code[1]), "accept": "Yes"}
                continue
            form = next(
                ((a, f) for a, f in Forms(page.text).forms if a.get("action") and "accept" in f),
                None,
            )
            if form:
                attrs, fields = form
                fields.pop("cancel", None)
                fields["accept"] = fields.get("accept") or "Yes"
                url, method, data = urljoin(url, attrs["action"]), "POST", fields
                continue
            broker = re.search(
                r"""["']([^"'<>\s]*?/realms/[^"'<>\s]*?/broker/cas/login[^"'<>\s]*)["']""", text
            )
            if not broker:
                return False
            url, method, data = urljoin(url, broker[1]), "GET", None
        return False
    except Exception:
        # Page shapes and network diagnostics may contain credentials.
        return False
