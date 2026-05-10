"""Run-once environment normalization shared by `inspire account add` and
high-risk command entry points.

Designed to keep the rest of the CLI free of compat branches: anything
left over from pre-v3 installs (Inspire-cli 0.2.4, InspireSkill v1/v2)
is quarantined and announced once; stale env vars dropped by v3.x get
flagged; SSO browser deps get checked. The main code paths then assume
a clean v3.x layout without scattered ``if old_format`` guards.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from inspire.accounts.storage import inspire_home

NORMALIZATION_SENTINEL = ".environment-normalized-v3"

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

    ``interactive=True`` permits printing reminders to stderr; pair with
    ``auto_install_playwright=True`` from `inspire account add` to offer
    an automatic ``playwright install chromium`` when the browser is
    missing. Other entry points should call with both False.
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
            report.playwright_ready = True

    # Quarantining is destructive (rename), so we announce it even when not
    # interactive — scripted callers still need to know which files moved.
    # The remaining observations (stale env, missing playwright) only print
    # under interactive mode where the user can act on them.
    if report.quarantined:
        _print_quarantine_notice(report)
    if interactive and (report.stale_env_vars or not report.playwright_ready):
        _print_remaining_observations(report)

    return report


def _quarantine_if_present(path: Path, report: NormalizationReport) -> None:
    if not path.exists():
        return
    target = path.with_name(path.name + ".legacy")
    path.rename(target)
    report.quarantined.append((path, target))


def _print_quarantine_notice(report: NormalizationReport) -> None:
    for orig, new in report.quarantined:
        print(
            f"  Quarantined legacy file: {orig} → {new.name}",
            file=sys.stderr,
        )


def _print_remaining_observations(report: NormalizationReport) -> None:
    for env in report.stale_env_vars:
        print(
            f"  Stale env var '{env}' is set but ignored by v3.x. "
            f"Run `unset {env}` (or remove from your shell rc) to silence this.",
            file=sys.stderr,
        )
    if not report.playwright_ready:
        if report.playwright_install_attempted and not report.playwright_install_succeeded:
            print(
                "  Playwright chromium install failed. SSO login will fail until you run "
                "manually:\n"
                "    playwright install chromium\n"
                "    # or, when InspireSkill is installed via uv tool:\n"
                "    uvx --from inspire-skill playwright install chromium",
                file=sys.stderr,
            )
        else:
            print(
                "  Playwright chromium not detected. SSO login will need it. Install with:\n"
                "    playwright install chromium\n"
                "    # or, when InspireSkill is installed via uv tool:\n"
                "    uvx --from inspire-skill playwright install chromium",
                file=sys.stderr,
            )


def _playwright_chromium_available() -> bool:
    """Best-effort check that Playwright's chromium browser is on disk.

    Cheap: globs the well-known cache locations rather than spawning the
    Playwright Node.js host (which costs >1 s per invocation).
    """
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False

    custom = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    candidates: list[Path] = []
    if custom and custom != "0":
        candidates.append(Path(custom))
    candidates.extend(
        (
            Path.home() / "Library" / "Caches" / "ms-playwright",
            Path.home() / ".cache" / "ms-playwright",
            Path.home() / "AppData" / "Local" / "ms-playwright",
        )
    )
    for cache_dir in candidates:
        if cache_dir.exists() and any(cache_dir.glob("chromium*")):
            return True
    return False


def _install_playwright_chromium(timeout_s: int = 600) -> bool:
    """Attempt ``playwright install chromium``. Returns True on success.

    Tries the in-venv ``playwright`` binary first (works under ``uv tool
    install``); falls back to ``python -m playwright`` if the bin is not
    on PATH from this process.
    """
    candidates: list[list[str]] = []
    direct = shutil.which("playwright")
    if direct:
        candidates.append([direct, "install", "chromium"])
    candidates.append([sys.executable, "-m", "playwright", "install", "chromium"])

    for cmd in candidates:
        try:
            subprocess.run(cmd, check=True, timeout=timeout_s)
            return True
        except (subprocess.SubprocessError, OSError):
            continue
    return False


__all__ = [
    "NORMALIZATION_SENTINEL",
    "NormalizationReport",
    "normalize_environment",
]
