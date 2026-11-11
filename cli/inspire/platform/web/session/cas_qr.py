"""Feishu QR capture and module-sized terminal rendering."""

from __future__ import annotations

import os
import time
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
from .cas import CAS_HOST, LOGIN_URL, USER_AGENT, CasError, CasSession, cache_path
from .proxy import get_playwright_proxy


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


def feishu_qr_login(
    show_qr: Callable[[Path], None], account: str, timeout: float = 300
) -> CasSession:
    path = cache_path(account, "cas_qr.png")
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
                    expired = frame.query_selector(EXPIRED_QR_SELECTOR) if frame else None
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
                        if frame:
                            candidate = frame.query_selector(
                                'canvas, img[src*="qr"], img[class*="qr"]'
                            )
                            if candidate:
                                target = candidate
                        try:
                            png = target.screenshot(timeout=remaining_ms(5))
                        except PlaywrightError:
                            if captured:
                                continue
                            # A canvas can detach when Feishu rotates its QR.
                            fresh = page.query_selector("#larkQrcode iframe")
                            if fresh is None:
                                page.wait_for_timeout(remaining_ms(0.5))
                                continue
                            element = fresh
                            frame = fresh.content_frame()
                            png = fresh.screenshot(timeout=remaining_ms(5))
                        if png != previous:
                            path.parent.mkdir(parents=True, exist_ok=True)
                            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                            with os.fdopen(fd, "wb") as output:
                                os.chmod(path, 0o600)
                                output.write(png)
                            show_qr(path)
                            previous = png
                        refreshed = time.monotonic()
                    page.wait_for_timeout(remaining_ms(0.5))
                raise CasError("Feishu QR login timed out; run inspire login --cas feishu again.")
            finally:
                browser.close()
    except CasError:
        raise
    except Exception as error:
        if is_playwright_browser_runtime_error(error):
            raise CasError(
                f"Playwright Chromium could not start; run {playwright_install_hint()}."
            ) from None
        raise CasError("Feishu QR login failed; retry inspire login --cas feishu.") from None
    finally:
        path.unlink(missing_ok=True)
