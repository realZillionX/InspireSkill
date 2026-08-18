"""Local browser-runtime setup used by account creation and init."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from inspire.platform.web.session.browser_launch import (
    playwright_install_args,
    playwright_install_hint,
)

logger = logging.getLogger(__name__)


@dataclass
class NormalizationReport:
    playwright_ready: bool = True
    playwright_install_attempted: bool = False
    playwright_install_succeeded: bool = False


def normalize_environment(
    *,
    interactive: bool = False,
    auto_install_playwright: bool = False,
) -> NormalizationReport:
    """Check and, when requested, install the local Chromium runtime.

    Account creation and initialization use this as a focused readiness check;
    it only inspects the local browser runtime and performs an installation
    when explicitly requested.
    """
    report = NormalizationReport()

    report.playwright_ready = _playwright_chromium_available()
    if not report.playwright_ready and interactive and auto_install_playwright:
        report.playwright_install_attempted = True
        report.playwright_install_succeeded = _install_playwright_chromium()
        if report.playwright_install_succeeded:
            report.playwright_ready = _playwright_chromium_available()

    _log_runtime_report(report)
    if interactive and not report.playwright_ready:
        from inspire.cli.context import Context
        from inspire.cli.utils.errors import emit_error

        emit_error(
            Context(),
            error_type="EnvironmentNotice",
            message="Playwright Chromium is not ready.",
            exit_code=0,
            human_lines=[
                "Playwright Chromium is not ready. "
                f"Run `{playwright_install_hint()}` "
                "before browser login."
            ],
        )

    return report


def _log_runtime_report(report: NormalizationReport) -> None:
    if not report.playwright_ready:
        if report.playwright_install_attempted and not report.playwright_install_succeeded:
            logger.debug(
                "Playwright Chromium setup failed; repair command: %s",
                playwright_install_hint(),
            )
        elif report.playwright_install_attempted and report.playwright_install_succeeded:
            logger.debug(
                "Playwright Chromium installed but launch probe failed; repair command: %s",
                playwright_install_hint(),
            )
        else:
            logger.debug(
                "Playwright Chromium not detected; setup command: %s",
                playwright_install_hint(),
            )


def _playwright_chromium_available() -> bool:
    """Best-effort check that Playwright's Chromium runtime can launch."""
    try:
        from playwright.sync_api import sync_playwright
        from inspire.platform.web.session.browser_launch import chromium_launch_kwargs
    except ImportError:
        return False

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**chromium_launch_kwargs(headless=True))
            browser.close()
        return True
    except Exception:
        return False


def _current_environment_playwright_bin() -> str | None:
    candidate = Path(sys.executable).with_name("playwright")
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _install_playwright_chromium(
    timeout_s: int = 600,
    *,
    include_system_deps: bool | None = False,
) -> bool:
    """Attempt Playwright Chromium installation. Returns True on success.

    Tries the in-venv ``playwright`` binary first; falls back to the current
    package interpreter if the bin is not on PATH from this process.
    """
    candidates: list[list[str]] = []
    # Account setup should not mutate the base image's apt layer. It may
    # download the browser binary, but Linux system dependencies are installed
    # only from `inspire init`, after a launch probe fails and the user accepts.
    install_args = playwright_install_args(include_system_deps=include_system_deps)
    direct = _current_environment_playwright_bin() or shutil.which("playwright")
    if direct:
        candidates.append([direct, *install_args])
    candidates.append([sys.executable, "-m", "playwright", *install_args])

    for cmd in candidates:
        try:
            subprocess.run(
                cmd,
                check=True,
                timeout=timeout_s,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            return True
        except subprocess.CalledProcessError as err:
            logger.debug(
                "Playwright install failed: command=%r stdout=%r stderr=%r",
                cmd,
                err.stdout,
                err.stderr,
            )
        except subprocess.TimeoutExpired as err:
            logger.debug(
                "Playwright install timed out: command=%r stdout=%r stderr=%r",
                cmd,
                err.stdout,
                err.stderr,
            )
        except OSError as err:
            logger.debug("Playwright install could not start: command=%r error=%s", cmd, err)
            continue
    return False


__all__ = [
    "NormalizationReport",
    "normalize_environment",
]
