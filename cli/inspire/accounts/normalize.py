"""Run-once environment normalization shared by `inspire account add` and
high-risk command entry points.

Designed to keep the rest of the CLI free of compat branches: anything
left over from pre-v3 installs (Inspire-cli 0.2.4, InspireSkill v1/v2)
is quarantined and announced once; stale env vars dropped by v3.x get
flagged; SSO browser deps get checked. The main code paths then assume
a clean v3.x layout without scattered ``if old_format`` guards.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from inspire.accounts.storage import inspire_home
from inspire.platform.web.session.browser_launch import (
    playwright_install_args,
    playwright_install_hint,
)

NORMALIZATION_SENTINEL = ".environment-normalized-v3"
logger = logging.getLogger(__name__)

_LEGACY_FILES_UNDER_INSPIRE_HOME = (
    ("bridges.json", "Pre-v3 SSH tunnel cache"),
    ("web_session.json", "Pre-v3 SSO session cache"),
    ("jobs.json", "Pre-v3 job cache"),
    ("config.toml", "Pre-v3 unscoped config"),
)

_LEGACY_FILES_UNDER_CACHE = (
    ("rtunnel-proxy-state.json", "Pre-v3 rtunnel proxy state"),
)

_LEGACY_ENV_VARS: tuple[str, ...] = ()


def _cache_root() -> Path:
    return Path.home() / ".cache" / "inspire-skill"


@dataclass
class NormalizationReport:
    quarantined: list[tuple[Path, Path]] = field(default_factory=list)
    stale_env_vars: list[str] = field(default_factory=list)
    playwright_ready: bool = True
    playwright_install_attempted: bool = False
    playwright_install_succeeded: bool = False

    @property
    def has_observations(self) -> bool:
        return bool(self.quarantined or self.stale_env_vars or not self.playwright_ready)


def normalize_environment(
    *,
    interactive: bool = False,
    auto_install_playwright: bool = False,
) -> NormalizationReport:
    """Run all once-off environment normalization tasks. Idempotent.

    A single sentinel file at ``~/.inspire/.environment-normalized-v3`` flips
    the file-quarantine pass off after first success. The env-var scan and
    playwright check run every time (cheap; users may flip these between
    invocations).

    ``interactive=True`` permits automatic browser runtime setup when paired
    with ``auto_install_playwright=True``. Detailed observations are returned
    to callers and written to the local debug log; any concise actionable
    notice goes through the shared sanitized CLI emitter.
    """
    report = NormalizationReport()

    home = inspire_home()
    home.mkdir(parents=True, exist_ok=True)
    sentinel = home / NORMALIZATION_SENTINEL

    # Atomic claim of the "I'm the one running quarantine" right.
    # Two concurrent `inspire account add` invocations would otherwise race
    # on `path.exists()` / `path.rename()` — the second one would fall
    # through to FileNotFoundError mid-quarantine. `O_CREAT | O_EXCL` lets
    # exactly one process win; the loser sees the sentinel already there
    # and skips the file pass entirely (its observations are already
    # encoded in the .legacy copies the winner produced).
    we_own_quarantine = False
    if not sentinel.exists():
        try:
            fd = os.open(
                str(sentinel),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
            os.close(fd)
            we_own_quarantine = True
        except FileExistsError:
            we_own_quarantine = False

    if we_own_quarantine:
        for filename, _label in _LEGACY_FILES_UNDER_INSPIRE_HOME:
            _quarantine_if_present(home / filename, report)
        cache = _cache_root()
        if cache.exists():
            for filename, _label in _LEGACY_FILES_UNDER_CACHE:
                _quarantine_if_present(cache / filename, report)
        # v2.x stored playwright sessions under `accounts/<n>/sessions/`;
        # v3.x replaced the directory with a single `web_session.json`. If
        # someone upgrades from v2.x, the old directory is just dead state
        # taking up a slot in the account folder — quarantine it the same
        # way to keep the directory listing clean. Quarantine target lands
        # next to the directory, not inside the account, so a future
        # `account remove` doesn't drag it along.
        accounts_root = home / "accounts"
        if accounts_root.is_dir():
            for account_path in accounts_root.iterdir():
                if not account_path.is_dir():
                    continue
                legacy_sessions = account_path / "sessions"
                if not legacy_sessions.is_dir():
                    continue
                target = account_path / "sessions.legacy"
                if target.exists():
                    continue
                legacy_sessions.rename(target)
                report.quarantined.append((legacy_sessions, target))

    for env in _LEGACY_ENV_VARS:
        if os.environ.get(env, "").strip():
            report.stale_env_vars.append(env)

    report.playwright_ready = _playwright_chromium_available()
    if not report.playwright_ready and interactive and auto_install_playwright:
        report.playwright_install_attempted = True
        report.playwright_install_succeeded = _install_playwright_chromium()
        if report.playwright_install_succeeded:
            report.playwright_ready = _playwright_chromium_available()

    _log_normalization_report(report)
    if interactive and not report.playwright_ready:
        from inspire.cli.context import Context
        from inspire.cli.utils.output import emit_error

        emit_error(
            Context(),
            error_type="EnvironmentNotice",
            message="Playwright Chromium is not ready.",
            exit_code=0,
            human_lines=[
                "Playwright Chromium is not ready. "
                f"Run `{playwright_install_hint(include_system_deps=False)}` "
                "before browser login."
            ],
        )

    return report


def _quarantine_if_present(path: Path, report: NormalizationReport) -> None:
    if not path.exists():
        return
    target = path.with_name(path.name + ".legacy")
    path.rename(target)
    report.quarantined.append((path, target))


def _log_normalization_report(report: NormalizationReport) -> None:
    for orig, new in report.quarantined:
        logger.debug("Quarantined legacy path: %s -> %s", orig, new)
    for env in report.stale_env_vars:
        logger.debug("Ignored stale environment variable: %s", env)
    if not report.playwright_ready:
        if report.playwright_install_attempted and not report.playwright_install_succeeded:
            logger.debug(
                "Playwright Chromium setup failed; repair command: %s",
                playwright_install_hint(include_system_deps=False),
            )
        elif report.playwright_install_attempted and report.playwright_install_succeeded:
            logger.debug(
                "Playwright Chromium installed but launch probe failed; repair command: %s",
                playwright_install_hint(),
            )
        else:
            logger.debug(
                "Playwright Chromium not detected; setup command: %s",
                playwright_install_hint(include_system_deps=False),
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
    "NORMALIZATION_SENTINEL",
    "NormalizationReport",
    "normalize_environment",
]
