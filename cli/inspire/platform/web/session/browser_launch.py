"""Shared Playwright Chromium launch options."""

from __future__ import annotations

import os
import shutil
import sys
from typing import Any


CHROMIUM_CONTAINER_ARGS = (
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
)
CHROMIUM_EXECUTABLE_ENV = "INSPIRE_PLAYWRIGHT_CHROMIUM_EXECUTABLE"
CHROMIUM_CHANNEL_ENV = "INSPIRE_PLAYWRIGHT_CHROMIUM_CHANNEL"


def should_install_playwright_system_deps() -> bool:
    """Return true when Playwright can repair Linux browser dependencies.

    Inspire notebooks commonly run as root in minimal Ubuntu containers. In
    that environment ``playwright install --with-deps chromium`` can install
    missing shared libraries such as libglib in the same one-time setup step.
    """
    if not sys.platform.startswith("linux"):
        return False
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return False
    return shutil.which("apt-get") is not None


def playwright_install_args(*, include_system_deps: bool | None = None) -> list[str]:
    """Return arguments for the Playwright CLI install command."""
    if include_system_deps is None:
        include_system_deps = should_install_playwright_system_deps()
    args = ["install"]
    if include_system_deps:
        args.append("--with-deps")
    args.append("chromium")
    return args


def playwright_install_hint(*, include_system_deps: bool | None = None) -> str:
    """Return an install command suitable for user-facing diagnostics."""
    return "inspire update --cli-only"


def is_playwright_browser_runtime_error(exc: BaseException) -> bool:
    """Return true for browser executable or system-library startup failures."""
    text = str(exc)
    return any(
        marker in text
        for marker in (
            "Playwright Chromium could not start",
            "BrowserType.launch:",
            "Executable doesn't exist",
            "error while loading shared libraries",
            "Host system is missing dependencies",
        )
    )


def chromium_launch_kwargs(*, headless: bool = True, proxy: Any = None) -> dict[str, Any]:
    """Return Chromium launch kwargs that also work in Inspire containers.

    Inspire notebooks commonly run as root inside containers with a small
    ``/dev/shm``. Chromium can start successfully and then close the page
    process on first navigation unless these compatibility flags are present.
    """
    kwargs: dict[str, Any] = {
        "headless": headless,
        "args": list(CHROMIUM_CONTAINER_ARGS),
    }
    executable_path = os.environ.get(CHROMIUM_EXECUTABLE_ENV, "").strip()
    channel = os.environ.get(CHROMIUM_CHANNEL_ENV, "").strip()
    if executable_path:
        kwargs["executable_path"] = executable_path
    elif channel:
        kwargs["channel"] = channel
    if proxy is not None:
        kwargs["proxy"] = proxy
    return kwargs


__all__ = [
    "CHROMIUM_CHANNEL_ENV",
    "CHROMIUM_CONTAINER_ARGS",
    "CHROMIUM_EXECUTABLE_ENV",
    "chromium_launch_kwargs",
    "is_playwright_browser_runtime_error",
    "playwright_install_args",
    "playwright_install_hint",
    "should_install_playwright_system_deps",
]
