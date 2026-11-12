"""Feishu QR capture and module-sized terminal rendering."""

from __future__ import annotations

import os
import time
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PIL import Image, ImageOps
from playwright.sync_api import Error as PlaywrightError, Route, sync_playwright

from .browser_launch import (
    chromium_launch_kwargs,
    is_playwright_browser_runtime_error,
    playwright_install_hint,
)
from .cas import (
    CAS_HOST, LOGIN_URL, USER_AGENT, CasError, CasSession, account_login_flow,
    cache_path, existing_account_write,
)
from .proxy import get_playwright_proxy
from .models import AuthenticationError


EXPIRED_QR_SELECTOR = ".qr-box-refresh, .scan-refresh-icon"
QR_CHECK_INTERVAL = 2.0


def render_qr(path: Path) -> str | None:
    """Sample a verified QR grid; retain a four-module white quiet zone."""
    with Image.open(path) as source:
        gray = source.convert("L")
    dark = gray.point(lambda p: 255 if p < 128 else 0)
    box = dark.getbbox()
    if box is None:
        return None
    dark = dark.crop(box)
    width, height = dark.size
    # Top finder border is exactly seven modules wide. Reject decorated captures.
    run = 0
    while run < width and dark.getpixel((run, 0)):
        run += 1
    module = run / 7
    if module < 1 or abs(width - height) > module:
        return None
    # Browser captures scale modules to fractional pixels; snap to the nearest
    # QR version and let the finder-pattern check below reject a wrong guess.
    size = 21 + 4 * round((width / module - 21) / 4)
    if size < 21 or size > 177:
        return None
    module = width / size
    grid = [
        [
            bool(
                dark.getpixel(
                    (
                        min(width - 1, int((x + 0.5) * module)),
                        min(height - 1, int((y + 0.5) * module)),
                    )
                )
            )
            for x in range(size)
        ]
        for y in range(size)
    ]
    for ox, oy in ((0, 0), (size - 7, 0), (0, size - 7)):
        for y in range(7):
            for x in range(7):
                expected = x in (0, 6) or y in (0, 6) or (2 <= x <= 4 and 2 <= y <= 4)
                if grid[oy + y][ox + x] != expected:
                    return None
    padded = Image.new("1", (size, size))
    padded.putdata([int(v) for row in grid for v in row])
    padded = ImageOps.expand(padded, border=4, fill=0)
    lines = []
    for y in range(0, padded.height, 2):
        line = ""
        for x in range(padded.width):
            top = bool(padded.getpixel((x, y)))
            bottom = y + 1 < padded.height and bool(padded.getpixel((x, y + 1)))
            line += {
                (False, False): " ",
                (True, False): "▀",
                (False, True): "▄",
                (True, True): "█",
            }[(top, bottom)]
        lines.append("\x1b[30;47m" + line + "\x1b[0m")
    return "\n".join(lines)


def _query(node: Any, selector: str) -> Any:
    """Query a page or frame, treating a navigation in progress as no match.

    After the scan is confirmed the Feishu iframe navigates to CAS; any query
    racing that navigation fails with "Execution context was destroyed" before
    the aip.sii.edu.cn redirect is intercepted, so keep polling instead.
    """
    if node is None:
        return None
    try:
        return node.query_selector(selector)
    except PlaywrightError:
        return None


def _publish_qr(account: str, path: Path | None, png: bytes) -> Path:
    with existing_account_write(account):
        directory = cache_path(account, "cas_qr.png").parent
        # The random staging name also reserves this login's destination name.
        fd, name = tempfile.mkstemp(dir=directory, prefix="cas_qr-", suffix=".png")
        temporary = Path(name)
        destination = path or temporary.with_name(temporary.stem + "-live.png")
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(png)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)


def feishu_qr_login(
    show_qr: Callable[[Path], None], account: str, timeout: float = 300
) -> CasSession:
    with account_login_flow(account):
        return _feishu_qr_login(show_qr, account, timeout)


def _feishu_qr_login(
    show_qr: Callable[[Path], None], account: str, timeout: float
) -> CasSession:
    path: Path | None = None
    deadline = time.monotonic() + timeout
    captured = False

    def remaining_ms(limit: float = 30) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CasError("Feishu QR login timed out; run inspire login --cas feishu again.")
        return min(limit, remaining) * 1000

    def intercept(route: Route) -> None:
        nonlocal captured
        captured = True
        route.fulfill(status=200, body="CAS sign-in complete", content_type="text/plain")

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                **chromium_launch_kwargs(proxy=get_playwright_proxy(account)),
                timeout=remaining_ms(),
            )
            try:
                context = browser.new_context(
                    user_agent=USER_AGENT, locale="zh-CN", viewport={"width": 1280, "height": 900}
                )
                context.route(lambda url: urlsplit(url).hostname == "aip.sii.edu.cn", intercept)
                page = context.new_page()
                page.goto(LOGIN_URL, wait_until="load", timeout=remaining_ms())
                page.evaluate("document.getElementById('appCode').click()")
                element = page.wait_for_selector("#larkQrcode iframe", timeout=remaining_ms(20))
                if element is None:
                    raise CasError("Feishu QR frame is unavailable.")
                frame = element.content_frame()
                if frame:
                    frame.wait_for_load_state("load", timeout=remaining_ms(20))
                page.wait_for_timeout(remaining_ms(2))
                refreshed = float("-inf")
                previous = b""
                while time.monotonic() < deadline:
                    if captured:
                        cookies: list[dict[str, Any]] = []
                        for cookie in context.cookies():
                            if cookie["domain"].lstrip(".") != CAS_HOST:
                                continue
                            cookies.append(
                                {
                                    k: dict(cookie)[k]
                                    for k in (
                                        "name",
                                        "value",
                                        "domain",
                                        "path",
                                        "expires",
                                        "secure",
                                    )
                                }
                            )
                            if cookies[-1]["expires"] <= 0:
                                cookies[-1]["expires"] = None
                        if any(c["name"] == "CASTGC" and c["value"] for c in cookies):
                            return CasSession(cookies, time.time(), "feishu")
                        raise CasError("Feishu login did not return a CAS session.")
                    # Feishu codes expire after about 60 s behind a refresh mask;
                    # click it so the user always sees a live code.
                    expired = _query(frame, EXPIRED_QR_SELECTOR)
                    if expired is not None:
                        try:
                            expired.click(timeout=remaining_ms(5))
                        except PlaywrightError:
                            pass
                        page.wait_for_timeout(remaining_ms(1.5))
                        refreshed = float("-inf")
                        continue
                    if time.monotonic() - refreshed >= QR_CHECK_INTERVAL:
                        target = element
                        candidate = _query(frame, 'canvas, img[src*="qr"], img[class*="qr"]')
                        if candidate:
                            target = candidate
                        try:
                            png = target.screenshot(timeout=remaining_ms(5))
                        except PlaywrightError:
                            if captured:
                                continue
                            # A canvas can detach when Feishu rotates its QR.
                            fresh = _query(page, "#larkQrcode iframe")
                            if fresh is None:
                                page.wait_for_timeout(remaining_ms(0.5))
                                continue
                            try:
                                frame = fresh.content_frame()
                                png = fresh.screenshot(timeout=remaining_ms(5))
                            except PlaywrightError:
                                page.wait_for_timeout(remaining_ms(0.5))
                                continue
                            element = fresh
                        # The mask can appear between the check above and the
                        # capture; never publish an expired code.
                        if _query(frame, EXPIRED_QR_SELECTOR) is not None:
                            continue
                        if png != previous:
                            path = _publish_qr(account, path, png)
                            show_qr(path)
                            previous = png
                        refreshed = time.monotonic()
                    page.wait_for_timeout(remaining_ms(0.5))
                raise CasError("Feishu QR login timed out; run inspire login --cas feishu again.")
            finally:
                browser.close()
    except AuthenticationError:
        raise
    except Exception as error:
        if is_playwright_browser_runtime_error(error):
            raise CasError(
                f"Playwright Chromium could not start; run {playwright_install_hint()}."
            ) from None
        raise CasError("Feishu QR login failed; retry inspire login --cas feishu.") from None
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
