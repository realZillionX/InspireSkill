"""Private cookie jars for application HTTP under the caller's request policy.

TensorBoard data and Jupyter Contents need application cookies without adding
them to the console session. A borrowed connection routes sends through Transport
for deadlines, renewal and single-send classification, but returns a response
object rather than decoding a console envelope. Platform-cookie renewal preserves
application cookies; a rejected application cookie is not proof of a failed write.
"""

from __future__ import annotations

from typing import Any
from inspire.platform.web.transport_core import ApplicationRequest


class ApplicationConnection:
    def __init__(self, transport: Any, http: Any, generation: float) -> None:
        self.transport, self.http, self.generation = transport, http, generation
        self.platform_cookie_names = {cookie.name for cookie in http.cookies}

    def reconfigure(self, session: Any, url: str) -> None:
        from inspire.platform.web.session.requests import _configure

        application_cookies = [
            cookie for cookie in self.http.cookies if cookie.name not in self.platform_cookie_names
        ]
        _configure(self.http, session, url)
        self.platform_cookie_names = {cookie.name for cookie in self.http.cookies}
        for cookie in application_cookies:
            self.http.cookies.set_cookie(cookie)
        self.generation = session.created_at

    @property
    def cookies(self) -> Any:
        return self.http.cookies

    def request(self, method: str, url: str, **options: Any) -> Any:
        allow_not_found = options.pop("allow_not_found", False)
        timeout = options.pop("timeout", 30)
        if isinstance(timeout, tuple):
            timeout = timeout[1]
        options.setdefault("allow_redirects", False)
        return self.transport.request(
            method,
            url,
            body=ApplicationRequest(self, options, allow_not_found=allow_not_found),
            timeout=timeout,
        )

    def get(self, url: str, **options: Any) -> Any:
        return self.request("GET", url, **options)

    def post(self, url: str, **options: Any) -> Any:
        return self.request("POST", url, **options)

    def delete(self, url: str, **options: Any) -> Any:
        return self.request("DELETE", url, **options)
