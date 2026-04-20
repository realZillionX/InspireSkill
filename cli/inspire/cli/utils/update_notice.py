"""Update check / notice helpers for the `inspire` CLI.

The startup hook in `inspire.cli.main` calls `maybe_notify_update()` and
`maybe_spawn_check()` on every invocation. Both must be cheap and
completely side-effect-free on failure — they never raise, never block
meaningful latency, and never write to stdout unless a newer version
is actually available.

Cache file: ~/.inspire/update-status.json
Source of truth: cli/pyproject.toml on `main` (parsed via raw.githubusercontent.com).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from inspire import __version__

REPO_SLUG = "realZillionX/InspireSkill"
PACKAGE_NAME = "inspire-skill"
RAW_PYPROJECT_URL = f"https://raw.githubusercontent.com/{REPO_SLUG}/main/cli/pyproject.toml"
TARBALL_URL = f"https://codeload.github.com/{REPO_SLUG}/tar.gz/refs/heads/main"

CACHE_DIR = Path.home() / ".inspire"
CACHE_FILE = CACHE_DIR / "update-status.json"
CHECK_TTL_SECONDS = 24 * 3600
FETCH_TIMEOUT = 6  # seconds — foreground check is bounded

_VERSION_RE = re.compile(r'^\s*version\s*=\s*"([^"]+)"', re.MULTILINE)
_SKIP_ENV = "INSPIRE_SKIP_UPDATE_CHECK"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_cache() -> dict[str, Any] | None:
    try:
        with CACHE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _write_cache(data: dict[str, Any]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        tmp.replace(CACHE_FILE)
    except OSError:
        pass


def _cache_age_seconds(cache: dict[str, Any]) -> float | None:
    checked_at = cache.get("checked_at")
    if not isinstance(checked_at, str):
        return None
    try:
        ts = datetime.strptime(checked_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - ts).total_seconds()


def _parse_version(toml_text: str) -> str | None:
    m = _VERSION_RE.search(toml_text)
    return m.group(1) if m else None


def _version_tuple(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in v.split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        if num:
            parts.append(int(num))
    return tuple(parts)


def _is_newer(latest: str, current: str) -> bool:
    try:
        return _version_tuple(latest) > _version_tuple(current)
    except Exception:
        return latest != current


def fetch_latest_version() -> str | None:
    """Hit the raw pyproject.toml on `main` and parse its version. Returns None on any failure."""
    req = urllib.request.Request(
        RAW_PYPROJECT_URL,
        headers={"User-Agent": f"inspire-skill/{__version__}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    return _parse_version(body)


def run_check(write: bool = True) -> dict[str, Any]:
    """Perform a fresh version check and (by default) persist the result."""
    latest = fetch_latest_version()
    result: dict[str, Any] = {
        "current": __version__,
        "latest": latest,
        "checked_at": _now_iso(),
        "source": RAW_PYPROJECT_URL,
    }
    if write:
        _write_cache(result)
    return result


def maybe_notify_update() -> None:
    """Print a one-line upgrade reminder to stderr if the cache says a newer version exists.

    Silent if no cache, parse fails, or we're already on the latest version.
    """
    if os.environ.get(_SKIP_ENV) == "1":
        return
    cache = _read_cache()
    if not cache:
        return
    latest = cache.get("latest")
    current = cache.get("current") or __version__
    if not isinstance(latest, str) or not latest:
        return
    if not _is_newer(latest, __version__):
        return
    try:
        # ANSI yellow; Click will handle non-tty gracefully when we print via click.echo,
        # but we go through sys.stderr to avoid touching click's context here.
        sys.stderr.write(
            f"\033[33m⚠ InspireSkill v{latest} available (current v{current}); "
            f"run `inspire update` to upgrade.\033[0m\n"
        )
    except Exception:
        pass


def maybe_spawn_check() -> None:
    """If the cache is stale or missing, fire off a detached `inspire update --check --silent`.

    Never waits, never raises. The child runs with INSPIRE_SKIP_UPDATE_CHECK=1 so it
    does not spawn another check recursively.
    """
    if os.environ.get(_SKIP_ENV) == "1":
        return
    cache = _read_cache()
    if cache is not None:
        age = _cache_age_seconds(cache)
        if age is not None and age < CHECK_TTL_SECONDS:
            return

    # Resolve the `inspire` entry point the child should run. Prefer the same
    # interpreter we're running under so uv tool / pipx / editable installs
    # all work without relying on PATH resolution.
    cmd = [sys.executable, "-m", "inspire.cli.main", "update", "--check", "--silent"]
    env = os.environ.copy()
    env[_SKIP_ENV] = "1"
    try:
        devnull = subprocess.DEVNULL
        subprocess.Popen(
            cmd,
            stdin=devnull,
            stdout=devnull,
            stderr=devnull,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        pass
