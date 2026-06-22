"""`inspire update` — check for and install newer InspireSkill versions.

Covers two things a user might want:

    inspire update                 # full upgrade: CLI package + SKILL/references
    inspire update --check         # only check upstream; write cache; print status
    inspire update --silent        # suppress output (used by the background check)
    inspire update --cli-only      # upgrade the Python package and runtime only
    inspire update --skill-only    # refresh SKILL.md + references/ only

Design notes:
- Upstream version comes from cli/pyproject.toml on main (parsed via raw.githubusercontent.com).
- SKILL/references are copied (not symlinked) into every detected harness skills dir.
- The Python package is upgraded via whatever installer currently owns it
  (`uv tool upgrade` / `pipx upgrade`), detected from ``sys.executable``'s
  path. ``inspire-skill`` is published to PyPI, so the standard upgrade path
  works — the `install.sh` default SPEC is also the PyPI package name, so
  first-time install and `inspire update` pull from the same source.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import click

from inspire import __version__
from inspire.cli.utils.update_notice import (
    REPO_SLUG,
    PACKAGE_NAME,
    TARBALL_URL,
    run_check,
    _is_newer,
    _version_tuple,
)
from inspire.accounts.normalize import (
    _install_playwright_chromium,
    _playwright_chromium_available,
)
from inspire.platform.web.session.browser_launch import playwright_install_args


def _opencode_config_dir() -> Path:
    """Resolve OpenCode's config dir: $OPENCODE_CONFIG_DIR or ~/.config/opencode."""
    override = os.environ.get("OPENCODE_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "opencode"


HARNESS_SKILL_DIRS = {
    "claude": Path.home() / ".claude" / "skills" / "inspire",
    "codex": Path.home() / ".codex" / "skills" / "inspire",
    "antigravity": Path.home() / ".gemini" / "config" / "skills" / "inspire",
    "cursor": Path.home() / ".cursor" / "skills" / "inspire",
    "openclaw": Path.home() / ".openclaw" / "skills" / "inspire",
    "opencode": _opencode_config_dir() / "skills" / "inspire",
    "qoder": Path.home() / ".qoder" / "skills" / "inspire",
    "kimi-code": Path.home() / ".kimi-code" / "skills" / "inspire",
}
HARNESS_LEGACY_SKILL_DIRS = {
    "antigravity": [Path.home() / ".gemini" / "skills" / "inspire"],
}
HARNESS_ROOTS = {
    "claude": Path.home() / ".claude",
    "codex": Path.home() / ".codex",
    "antigravity": Path.home() / ".gemini",
    "cursor": Path.home() / ".cursor",
    "openclaw": Path.home() / ".openclaw",
    "opencode": _opencode_config_dir(),
    "qoder": Path.home() / ".qoder",
    "kimi-code": Path.home() / ".kimi-code",
}

SKILL_ASSETS = ("SKILL.md", "references")
INSTALL_STATE_FILE = ".inspire-skill-install.json"

STALE_SKILL_PATTERNS = (
    ("INSPIRE_TARGET_DIR", "legacy target-dir environment variable"),
    ("env -u INSPIRE_PLAYWRIGHT_PROXY", "legacy proxy-unset command prefix"),
    ("-u INSPIRE_RTUNNEL_PROXY", "legacy rtunnel proxy command prefix"),
    ("uvx --from inspire-skill playwright", "low-level Playwright runtime repair command"),
    ("[paths].target_dir", "legacy target_dir config path"),
    ("Missing target directory configuration", "legacy target_dir error wording"),
)

PYPI_MIRROR_INDEX_URLS = (
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple",
    "https://mirrors.cloud.tencent.com/pypi/simple",
    "https://pypi.mirrors.ustc.edu.cn/simple",
)

NETWORK_OR_INDEX_ERROR_HINTS = (
    "failed to fetch",
    "request failed",
    "error sending request",
    "operation timed out",
    "timed out",
    "timeout",
    "temporary failure in name resolution",
    "name or service not known",
    "could not resolve",
    "connection reset",
    "connection refused",
    "network is unreachable",
    "tls",
    "ssl",
    "pypi.org/simple",
)

_UV_TOOL_LINE_RE = re.compile(
    rf"^{re.escape(PACKAGE_NAME)}\s+v(?P<version>\S+)"
    r"(?:\s+\[required:\s*(?P<required>[^\]]+)\])?"
    r"(?:\s+\((?P<env_path>[^)]+)\))?"
)
_UV_TOOL_EXEC_RE = re.compile(r"^-\s+inspire(?:\s+\((?P<path>[^)]+)\))?")
_VERSION_OUTPUT_RE = re.compile(r"\bversion\s+([0-9][^\s]*)")
GITHUB_RELEASES_API_URL = f"https://api.github.com/repos/{REPO_SLUG}/releases"
_CHANGELOG_RELEASE_HEADING_RE = re.compile(
    r"^#\s+(?P<tag>v?\d+(?:\.\d+){1,3}(?:[A-Za-z0-9._+-]*)?)\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class UvToolInfo:
    version: str | None = None
    required: str | None = None
    env_path: str | None = None
    executable_path: str | None = None


@dataclass(frozen=True)
class PipxToolInfo:
    version: str | None = None


@dataclass(frozen=True)
class ReleaseEntry:
    tag: str
    body: str
    url: str | None = None


def _detect_harnesses() -> list[str]:
    return [h for h, root in HARNESS_ROOTS.items() if root.is_dir()]


def _detect_installer() -> str | None:
    """Guess which installer owns the current `inspire` process.

    Probes ``sys.prefix`` (the venv root) — NOT ``sys.executable.resolve()``,
    because resolving the venv's ``python`` symlink follows it through to the
    underlying interpreter (e.g. ``~/.local/share/uv/python/cpython-3.11.../
    bin/python3``), which loses the ``tools`` segment that signals "this is a
    `uv tool install`". Same hazard applies to pipx — its venv python often
    resolves to the system Python and falls outside the pipx tree.

    Returns "uv", "pipx", or None (unknown / unsupported).
    """
    parts = Path(sys.prefix).parts
    if "uv" in parts and "tools" in parts:
        return "uv"
    if "pipx" in parts and "venvs" in parts:
        return "pipx"
    return None


def _parse_uv_tool_list(output: str) -> UvToolInfo | None:
    lines = output.splitlines()
    for index, line in enumerate(lines):
        match = _UV_TOOL_LINE_RE.match(line.strip())
        if not match:
            continue
        executable_path: str | None = None
        for child in lines[index + 1 :]:
            stripped = child.strip()
            if not stripped.startswith("- "):
                break
            exec_match = _UV_TOOL_EXEC_RE.match(stripped)
            if exec_match:
                executable_path = exec_match.group("path")
                break
        return UvToolInfo(
            version=match.group("version"),
            required=match.group("required"),
            env_path=match.group("env_path"),
            executable_path=executable_path,
        )
    return None


def _uv_tool_info() -> UvToolInfo | None:
    try:
        proc = subprocess.run(
            ["uv", "tool", "list", "--show-version-specifiers", "--show-paths"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    return _parse_uv_tool_list(proc.stdout or "")


def _pipx_tool_info() -> PipxToolInfo | None:
    try:
        proc = subprocess.run(
            ["pipx", "list", "--json"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return None
    venvs = payload.get("venvs")
    if not isinstance(venvs, dict) or PACKAGE_NAME not in venvs:
        return None
    meta = venvs.get(PACKAGE_NAME) or {}
    metadata = meta.get("metadata") if isinstance(meta, dict) else {}
    main_package = metadata.get("main_package") if isinstance(metadata, dict) else {}
    version = main_package.get("package_version") if isinstance(main_package, dict) else None
    return PipxToolInfo(version=version if isinstance(version, str) else None)


def _is_local_requirement(spec: str | None) -> bool:
    if not spec:
        return False
    value = spec.strip()
    if value.startswith("file://"):
        return True
    if value.startswith(("/", "./", "../", "~")):
        return True
    return " @ file://" in value


_SAFE_VERSION_RE = re.compile(r"^[0-9][A-Za-z0-9.!+_-]*$")


def _package_requirement(target_version: str | None = None) -> str:
    if target_version and _SAFE_VERSION_RE.match(target_version):
        return f"{PACKAGE_NAME}=={target_version}"
    return PACKAGE_NAME


def _official_uv_install_cmd(target_version: str | None = None) -> list[str]:
    # `uv tool upgrade` preserves the original install requirement. If the
    # tool was installed from a local path, that keeps updating from the local
    # checkout. For a global end-user update, force the canonical PyPI package
    # requirement so `inspire update` can repair local-path installs in one run.
    return ["uv", "tool", "install", "--force", "--refresh", _package_requirement(target_version)]


def _is_likely_network_or_index_error(output: str) -> bool:
    text = output.lower()
    return any(hint in text for hint in NETWORK_OR_INDEX_ERROR_HINTS)


def _upgrade_env_with_index(index_url: str) -> dict[str, str]:
    env = os.environ.copy()
    # uv reads UV_DEFAULT_INDEX; pipx shells out to pip, which reads
    # PIP_INDEX_URL. Set both so the retry path works for either installer
    # without changing the user's global config.
    env["UV_DEFAULT_INDEX"] = index_url
    env["PIP_INDEX_URL"] = index_url
    return env


def _run_upgrade_command(
    cmd: list[str],
    *,
    silent: bool,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        check=False,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    if not silent:
        if proc.stdout:
            click.echo(proc.stdout, nl=False)
        if proc.stderr:
            click.echo(proc.stderr, nl=False, err=True)
    return proc.returncode, output


def _upgrade_cli(silent: bool, target_version: str | None = None) -> bool:
    installer = _detect_installer()
    uv_info = None if silent and installer in {"uv", "pipx"} else _uv_tool_info()
    if installer == "uv":
        cmd = _official_uv_install_cmd(target_version)
        if uv_info and _is_local_requirement(uv_info.required) and not silent:
            click.secho(
                f"! Existing uv tool install uses a local source ({uv_info.required}); "
                "resetting the global tool to the official PyPI package.",
                fg="yellow",
                err=True,
            )
    elif installer == "pipx":
        cmd = ["pipx", "upgrade", PACKAGE_NAME]
    elif uv_info is not None:
        cmd = _official_uv_install_cmd(target_version)
        if not silent:
            click.secho(
                "› Current process is not the global uv tool install; "
                "updating the global `inspire` executable managed by uv.",
                fg="blue",
            )
            if _is_local_requirement(uv_info.required):
                click.secho(
                    f"! Existing uv tool install uses a local source ({uv_info.required}); "
                    "resetting the global tool to the official PyPI package.",
                    fg="yellow",
                    err=True,
                )
    elif _pipx_tool_info() is not None:
        cmd = ["pipx", "upgrade", PACKAGE_NAME]
        if not silent:
            click.secho(
                "› Current process is not the global pipx install; "
                "updating the global `inspire` executable managed by pipx.",
                fg="blue",
            )
    else:
        if not silent:
            click.secho(
                "✗ Can't find a global InspireSkill install managed by "
                "`uv tool install` or `pipx install`.",
                fg="red",
                err=True,
            )
            click.secho(f"  python = {sys.executable}", fg="red", err=True)
            click.secho(f"  prefix = {sys.prefix}", fg="red", err=True)
            click.echo(
                "\n  Reinstall through the official installer so future updates "
                "use the same path as first-time installs:\n"
                "      curl -fsSL https://raw.githubusercontent.com/realZillionX/InspireSkill/main/scripts/install.sh | bash",
                err=True,
            )
        return False

    if not silent:
        click.secho(f"› {' '.join(cmd)}", fg="blue")
    try:
        returncode, output = _run_upgrade_command(cmd, silent=silent)
    except FileNotFoundError:
        if not silent:
            click.secho(
                f"✗ `{cmd[0]}` not on PATH — you said this build was managed "
                f"by {cmd[0]} but the binary is gone.\n"
                f"  Reinstall via scripts/install.sh or run `{cmd[0]} --version` "
                f"to confirm.",
                fg="red",
                err=True,
            )
        return False

    if returncode == 0:
        return True

    if _is_likely_network_or_index_error(output):
        for index_url in PYPI_MIRROR_INDEX_URLS:
            if not silent:
                click.secho(
                    f"! PyPI/network error detected; retrying with mirror: {index_url}",
                    fg="yellow",
                    err=True,
                )
                click.secho(
                    f"› {' '.join(cmd)}  (UV_DEFAULT_INDEX/PIP_INDEX_URL={index_url})",
                    fg="blue",
                )
            try:
                retry_code, retry_output = _run_upgrade_command(
                    cmd,
                    silent=silent,
                    env=_upgrade_env_with_index(index_url),
                )
            except FileNotFoundError:
                if not silent:
                    click.secho(
                        f"✗ `{cmd[0]}` disappeared from PATH while retrying.",
                        fg="red",
                        err=True,
                    )
                return False
            if retry_code == 0:
                if not silent:
                    click.secho(f"✓ upgrade succeeded via mirror: {index_url}", fg="green")
                return True
            output += "\n" + retry_output

        if not silent:
            click.secho(
                f"✗ {cmd[0]} upgrade failed after trying PyPI and common mirrors.\n"
                "  If you are behind a proxy, enable the Clash virtual/TUN adapter "
                "or make sure your terminal inherits HTTP(S)_PROXY.\n"
                "  You can also configure a package mirror manually, for example:\n"
                "      UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple "
                f"{' '.join(cmd)}\n"
                "      PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple "
                f"{' '.join(cmd)}",
                fg="red",
                err=True,
            )
        return False

    if not silent:
        click.secho(
            f"✗ {cmd[0]} upgrade failed (exit {returncode}). "
            f"Run `{' '.join(cmd)}` manually to see the underlying message.",
            fg="red",
            err=True,
        )
    return False


def _ensure_playwright_runtime(silent: bool) -> bool:
    """Ensure the installed InspireSkill environment can launch Chromium."""
    if _playwright_chromium_available():
        if not silent:
            click.secho("✓ Playwright Chromium runtime verified", fg="green")
        return True

    if not silent:
        click.secho("› preparing Playwright Chromium runtime", fg="blue")
    if not _install_playwright_chromium(include_system_deps=None):
        if not silent:
            click.secho(
                "✗ Playwright Chromium runtime setup failed. Re-run `inspire update` "
                "after checking network access to the package and browser mirrors.",
                fg="red",
                err=True,
            )
        return False

    if _playwright_chromium_available():
        if not silent:
            click.secho("✓ Playwright Chromium runtime verified", fg="green")
        return True

    if not silent:
        click.secho(
            "✗ Playwright Chromium was installed but could not start in this environment. "
            "Re-run the standard installer after checking local browser support.",
            fg="red",
            err=True,
        )
    return False


def _global_inspire_executable() -> str | None:
    uv_info = _uv_tool_info()
    if uv_info and uv_info.executable_path:
        return uv_info.executable_path
    return shutil.which("inspire")


_PLAYWRIGHT_RUNTIME_PROBE = """
from inspire.platform.web.session.browser_launch import chromium_launch_kwargs
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(**chromium_launch_kwargs(headless=True))
    browser.close()
"""


def _emit_completed_process(proc: subprocess.CompletedProcess[str]) -> None:
    if proc.stdout:
        click.echo(proc.stdout, nl=False)
    if proc.stderr:
        click.echo(proc.stderr, nl=False, err=True)


def _run_runtime_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["INSPIRE_SKIP_UPDATE_CHECK"] = "1"
    return subprocess.run(
        cmd,
        check=False,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wrapper_python_from_inspire_executable(executable: str) -> str | None:
    try:
        with Path(executable).open("r", encoding="utf-8") as fh:
            shebang = fh.readline().strip()
    except OSError:
        return None
    if not shebang.startswith("#!"):
        return None
    try:
        parts = shlex.split(shebang[2:])
    except ValueError:
        return None
    if not parts:
        return None
    if Path(parts[0]).name == "env" and len(parts) >= 2:
        return shutil.which(parts[1])
    return parts[0] if Path(parts[0]).exists() else None


def _ensure_playwright_runtime_with_wrapper_python(executable: str, silent: bool) -> bool:
    python = _wrapper_python_from_inspire_executable(executable)
    if not python:
        if not silent:
            click.secho(
                f"✗ could not resolve the Python runtime behind `{executable}`.",
                fg="red",
                err=True,
            )
        return False

    install_proc = _run_runtime_command(
        [python, "-m", "playwright", *playwright_install_args(include_system_deps=None)],
    )
    if not silent:
        _emit_completed_process(install_proc)
    if install_proc.returncode != 0:
        return False

    probe_proc = _run_runtime_command([python, "-c", _PLAYWRIGHT_RUNTIME_PROBE])
    if not silent:
        _emit_completed_process(probe_proc)
    return probe_proc.returncode == 0


def _is_missing_runtime_hook(output: str) -> bool:
    return "_ensure-playwright-runtime" in output and "No such command" in output


def _ensure_global_playwright_runtime(silent: bool) -> bool:
    executable = _global_inspire_executable()
    if not executable:
        if not silent:
            click.secho(
                "✗ `inspire` is not on PATH after update, so runtime setup could not run.",
                fg="red",
                err=True,
            )
        return False

    cmd = [executable, "_ensure-playwright-runtime"]
    if silent:
        cmd.append("--silent")
    env = os.environ.copy()
    env["INSPIRE_SKIP_UPDATE_CHECK"] = "1"
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as e:
        if not silent:
            click.secho(f"✗ runtime setup could not start `{executable}`: {e}", fg="red", err=True)
        return False
    if proc.returncode == 0:
        if not silent:
            _emit_completed_process(proc)
        return True

    output = (proc.stdout or "") + (proc.stderr or "")
    if _is_missing_runtime_hook(output):
        if not silent:
            click.secho(
                "› installed CLI is older than the runtime setup hook; "
                "using the installed wrapper environment",
                fg="blue",
            )
        return _ensure_playwright_runtime_with_wrapper_python(executable, silent)

    if not silent:
        _emit_completed_process(proc)
    return False


def _run_post_update_command(
    *,
    previous_version: str,
    expected_version: str,
    cli_only: bool,
    silent: bool,
) -> bool:
    executable = _global_inspire_executable()
    if not executable:
        if not silent:
            click.secho(
                "✗ `inspire` is not on PATH after update, so post-update setup could not run.",
                fg="red",
                err=True,
            )
        return False

    cmd = [
        executable,
        "_post-update",
        "--previous-version",
        previous_version,
        "--expected-version",
        expected_version,
    ]
    if cli_only:
        cmd.append("--cli-only")
    if silent:
        cmd.append("--silent")

    env = os.environ.copy()
    env["INSPIRE_SKIP_UPDATE_CHECK"] = "1"
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as e:
        if not silent:
            click.secho(f"✗ post-update setup could not start `{executable}`: {e}", fg="red", err=True)
        return False
    if not silent:
        _emit_completed_process(proc)
    return proc.returncode == 0


def _download_tarball(timeout: int = 30, *, silent: bool = False) -> bytes | None:
    req = urllib.request.Request(
        TARBALL_URL,
        headers={"User-Agent": f"inspire-skill/{__version__}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        if not silent:
            click.secho(f"✗ tarball fetch failed: {e}", fg="red", err=True)
        return None


def _extract_assets(tarball: bytes, dest: Path) -> Path | None:
    """Extract the tarball into `dest` and return the top-level extracted dir.

    Defensive about two things:
    - **Top-level dir detection**: GitHub codeload tarballs always wrap
      content under a single ``<repo>-<ref>/`` directory, but we don't
      trust that ``members[0]`` is that directory entry — different tar
      tools order entries differently. Find the unique top segment by
      scanning all members.
    - **Path traversal**: pin ``filter='data'`` on Python 3.12+ where
      that's a documented safe default. Older Pythons silently use the
      legacy 'fully trusting' filter (``extractall`` without a filter
      kwarg), which is what we used before — codeload is GitHub-trusted
      so this is low-risk, but the explicit filter is strictly safer.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tf:
            members = tf.getmembers()
            if not members:
                return None
            top_segments = {m.name.split("/", 1)[0] for m in members if m.name}
            if len(top_segments) != 1:
                click.secho(
                    f"✗ tarball has unexpected layout (top-level dirs: {sorted(top_segments)}).",
                    fg="red",
                    err=True,
                )
                return None
            top = top_segments.pop()
            try:
                tf.extractall(dest, filter="data")
            except TypeError:
                # Python < 3.11.4 (no `filter=` kwarg). codeload is GitHub
                # which we trust, so the legacy extract is acceptable.
                tf.extractall(dest)
            extracted = dest / top
            return extracted if extracted.is_dir() else None
    except (tarfile.TarError, OSError) as e:
        click.secho(f"✗ tarball extract failed: {e}", fg="red", err=True)
        return None


def _iter_skill_files(root: Path) -> list[Path]:
    files: list[Path] = []
    skill_file = root / "SKILL.md"
    if skill_file.is_file():
        files.append(skill_file)
    refs = root / "references"
    if refs.is_dir():
        files.extend(path for path in refs.rglob("*") if path.is_file())
    return sorted(files)


def _scan_stale_skill_patterns(root: Path) -> list[str]:
    errors: list[str] = []
    for path in _iter_skill_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            errors.append(f"{path}: unreadable ({e})")
            continue
        for pattern, description in STALE_SKILL_PATTERNS:
            if pattern in text:
                errors.append(f"{path}: found {description} (`{pattern}`)")
    return errors


def _verify_skill_target(source_root: Path, target: Path) -> list[str]:
    errors: list[str] = []
    for source_path in _iter_skill_files(source_root):
        rel = source_path.relative_to(source_root)
        target_path = target / rel
        if not target_path.is_file():
            errors.append(f"{target_path}: missing after refresh")
            continue
        try:
            if target_path.read_bytes() != source_path.read_bytes():
                errors.append(f"{target_path}: content differs from refreshed source")
        except OSError as e:
            errors.append(f"{target_path}: unreadable after refresh ({e})")
    errors.extend(_scan_stale_skill_patterns(target))
    return errors


def _clean_legacy_skill_targets(harness: str, silent: bool) -> bool:
    ok = True
    target = HARNESS_SKILL_DIRS.get(harness)
    for legacy in HARNESS_LEGACY_SKILL_DIRS.get(harness, []):
        if target is not None and legacy == target:
            continue
        if not legacy.exists() and not legacy.is_symlink():
            continue
        try:
            if legacy.is_symlink() or legacy.is_file():
                legacy.unlink()
            else:
                shutil.rmtree(legacy)
            if not silent:
                click.secho(f"✓ removed legacy skill path → {legacy}", fg="green")
        except OSError as e:
            ok = False
            if not silent:
                click.secho(f"✗ couldn't clean legacy skill path {legacy}: {e}", fg="red", err=True)
    return ok


def _write_install_state(target: Path, *, latest_version: str | None = None) -> None:
    payload = {
        "package": PACKAGE_NAME,
        "version": latest_version or __version__,
        "source": TARBALL_URL,
        "assets": list(SKILL_ASSETS),
    }
    try:
        (target / INSTALL_STATE_FILE).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        # The copy verification below will catch serious write failures. The
        # manifest is a diagnostic convenience, not the source of truth.
        pass


def _refresh_skill_files(silent: bool, *, latest_version: str | None = None) -> bool:
    harnesses = _detect_harnesses()
    if not harnesses:
        if not silent:
            click.secho(
                "! No agent harness detected "
                "(checked ~/.claude, ~/.codex, ~/.gemini, ~/.cursor, ~/.openclaw, "
                "$OPENCODE_CONFIG_DIR or ~/.config/opencode, ~/.qoder, and ~/.kimi-code); "
                "skipping SKILL refresh.",
                fg="yellow",
                err=True,
            )
        return True  # not a failure; user may run skill-less

    tarball = _download_tarball()
    if tarball is None:
        return False

    with tempfile.TemporaryDirectory(prefix="inspire-skill-") as tmp:
        extracted = _extract_assets(tarball, Path(tmp))
        if extracted is None:
            click.secho("✗ tarball layout unexpected (no top-level dir).", fg="red", err=True)
            return False

        src_skill = extracted / "SKILL.md"
        src_refs = extracted / "references"
        if not src_skill.is_file():
            click.secho("✗ SKILL.md missing in tarball.", fg="red", err=True)
            return False

        for harness in harnesses:
            target = HARNESS_SKILL_DIRS[harness]
            if not _clean_legacy_skill_targets(harness, silent):
                return False
            # Wipe any previous install, including stale symlinks or files.
            if target.exists() or target.is_symlink():
                try:
                    if target.is_symlink() or target.is_file():
                        target.unlink()
                    else:
                        shutil.rmtree(target)
                except OSError as e:
                    click.secho(f"✗ couldn't clean {target}: {e}", fg="red", err=True)
                    return False
            target.mkdir(parents=True, exist_ok=True)

            shutil.copy2(src_skill, target / "SKILL.md")
            if src_refs.is_dir():
                shutil.copytree(src_refs, target / "references", dirs_exist_ok=True)
            _write_install_state(target, latest_version=latest_version)

            verify_errors = _verify_skill_target(extracted, target)
            if verify_errors:
                if not silent:
                    click.secho(f"✗ refreshed skill verification failed for {target}:", fg="red")
                    for error in verify_errors[:20]:
                        click.echo(f"  - {error}", err=True)
                    if len(verify_errors) > 20:
                        click.echo(f"  ... {len(verify_errors) - 20} more", err=True)
                return False

            if harness == "codex":
                agents_dir = target / "agents"
                agents_dir.mkdir(parents=True, exist_ok=True)
                (agents_dir / "openai.yaml").write_text(
                    'interface:\n'
                    '  display_name: "Inspire"\n'
                    '  short_description: "Execution-first Inspire operations via the inspire CLI, '
                    'including auth, proxy routing, notebook/image workflows, and job/HPC execution."\n',
                    encoding="utf-8",
                )

            if not silent:
                click.secho(f"✓ refreshed skill → {target}", fg="green")

    return True


def _normalize_release_version(version: str | None) -> str:
    return (version or "").strip().lstrip("v")


def _fetch_release_entries_from_github(timeout: int = 10) -> list[ReleaseEntry]:
    entries: list[ReleaseEntry] = []
    for page in range(1, 11):
        req = urllib.request.Request(
            f"{GITHUB_RELEASES_API_URL}?per_page=100&page={page}",
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"inspire-skill/{__version__}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            return entries
        if not isinstance(payload, list):
            return entries
        if not payload:
            return entries

        for item in payload:
            if not isinstance(item, dict) or item.get("draft"):
                continue
            tag = item.get("tag_name")
            if not isinstance(tag, str) or not tag.strip():
                continue
            body = item.get("body")
            url = item.get("html_url")
            entries.append(
                ReleaseEntry(
                    tag=tag.strip(),
                    body=body if isinstance(body, str) else "",
                    url=url if isinstance(url, str) else None,
                )
            )
        if len(payload) < 100:
            return entries
    return entries


def _release_entries_from_changelog_text(text: str) -> list[ReleaseEntry]:
    matches = list(_CHANGELOG_RELEASE_HEADING_RE.finditer(text))
    entries: list[ReleaseEntry] = []
    for index, match in enumerate(matches):
        tag = match.group("tag").strip()
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        entries.append(ReleaseEntry(tag=tag, body=body))
    return entries


def _changelog_text_from_tarball(tarball: bytes) -> str | None:
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tf:
            for member in tf.getmembers():
                if not member.isfile() or not member.name.endswith("/CHANGELOG.md"):
                    continue
                extracted = tf.extractfile(member)
                if extracted is None:
                    return None
                return extracted.read().decode("utf-8", errors="replace")
    except (tarfile.TarError, OSError):
        return None
    return None


def _fetch_release_entries_from_changelog(timeout: int = 10) -> list[ReleaseEntry]:
    tarball = _download_tarball(timeout=timeout, silent=True)
    if tarball is None:
        return []
    text = _changelog_text_from_tarball(tarball)
    if text is None:
        return []
    return _release_entries_from_changelog_text(text)


def _fetch_release_entries(timeout: int = 10) -> list[ReleaseEntry]:
    entries = _fetch_release_entries_from_github(timeout=timeout)
    if entries:
        return entries
    return _fetch_release_entries_from_changelog(timeout=timeout)


def _release_entries_between(
    entries: list[ReleaseEntry],
    *,
    previous_version: str,
    new_version: str,
) -> list[ReleaseEntry]:
    previous = _normalize_release_version(previous_version)
    new = _normalize_release_version(new_version)
    if not previous or not new or not _is_newer(new, previous):
        return []

    selected = [
        entry
        for entry in entries
        if _is_newer(_normalize_release_version(entry.tag), previous)
        and not _is_newer(_normalize_release_version(entry.tag), new)
    ]
    return sorted(
        selected,
        key=lambda entry: _version_tuple(_normalize_release_version(entry.tag)),
        reverse=True,
    )


def _release_body_for_display(body: str) -> str:
    lines = body.strip().splitlines()
    if lines and lines[0].strip() == "## 更新内容":
        lines = lines[1:]
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    return "\n".join(lines)


def _print_release_summary(previous_version: str, new_version: str, *, silent: bool) -> None:
    if silent or not _is_newer(new_version, previous_version):
        return

    entries = _release_entries_between(
        _fetch_release_entries(),
        previous_version=previous_version,
        new_version=new_version,
    )
    if not entries:
        click.secho(
            f"! 未能获取 v{previous_version} 到 v{new_version} 的 GitHub Release 更新内容。",
            fg="yellow",
            err=True,
        )
        return

    click.echo()
    click.secho(f"更新内容（v{previous_version} → v{new_version}）", bold=True)
    for entry in entries:
        version = _normalize_release_version(entry.tag)
        body = _release_body_for_display(entry.body)
        click.echo()
        click.secho(f"v{version}", bold=True)
        if body:
            click.echo(body)


def _normalize_after_success(silent: bool) -> None:
    try:
        from inspire.accounts import normalize_environment

        normalize_environment(interactive=not silent)
    except Exception:
        # Normalization is best-effort cleanup; never fail the upgrade itself.
        pass


def _run_post_update_tasks(
    *,
    expected_version: str | None,
    previous_version: str,
    cli_only: bool,
    silent: bool,
) -> bool:
    ok = True
    if not cli_only:
        ok = _refresh_skill_files(silent, latest_version=expected_version) and ok

    audit_ok, actual_version = _audit_update_state(
        expected_version=expected_version,
        check_cli=True,
        check_skills=not cli_only,
        silent=silent,
    )
    ok = audit_ok and ok

    if ok:
        ok = _ensure_global_playwright_runtime(silent) and ok

    run_check(write=True, current_version=actual_version or expected_version or __version__)

    if not ok:
        return False

    _normalize_after_success(silent)

    if not silent:
        click.secho("✓ InspireSkill updated.", fg="green", bold=True)

    new_version = str(actual_version or expected_version or __version__)
    _print_release_summary(previous_version, new_version, silent=silent)
    return True


def _parse_version_output(output: str) -> str | None:
    match = _VERSION_OUTPUT_RE.search(output)
    return match.group(1) if match else None


def _read_inspire_version(executable: str | None = None) -> tuple[str | None, str | None, str]:
    executable = executable or shutil.which("inspire")
    if not executable:
        return None, None, "not found on PATH"
    env = os.environ.copy()
    env["INSPIRE_SKIP_UPDATE_CHECK"] = "1"
    try:
        proc = subprocess.run(
            [executable, "--version"],
            check=False,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as e:
        return executable, None, str(e)
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return executable, None, output.strip() or f"exit {proc.returncode}"
    return executable, _parse_version_output(output), output.strip()


def _audit_global_cli(expected_version: str | None, silent: bool) -> tuple[bool, str | None]:
    uv_info = _uv_tool_info()
    executable_hint = uv_info.executable_path if uv_info and uv_info.executable_path else None
    executable, actual_version, detail = _read_inspire_version(executable_hint)
    ok = True
    if executable is None:
        ok = False
        if not silent:
            click.secho("✗ `inspire` is not on PATH after update.", fg="red", err=True)
    elif actual_version is None:
        ok = False
        if not silent:
            click.secho(
                f"✗ couldn't parse `{executable} --version` after update: {detail}",
                fg="red",
                err=True,
            )
    elif expected_version and _is_newer(expected_version, actual_version):
        ok = False
        if not silent:
            click.secho(
                f"✗ PATH executable is still v{actual_version}; expected v{expected_version}.",
                fg="red",
                err=True,
            )
            click.echo(f"  executable: {executable}", err=True)
    elif not silent:
        click.secho(f"✓ PATH inspire → {executable} (v{actual_version})", fg="green")

    if uv_info and _is_local_requirement(uv_info.required):
        ok = False
        if not silent:
            click.secho(
                f"✗ global uv tool install still points at local source: {uv_info.required}",
                fg="red",
                err=True,
            )
            click.echo("  Run `inspire update` again or reinstall with the official installer.", err=True)
    return ok, actual_version


def _audit_installed_skills(silent: bool) -> bool:
    ok = True
    for harness in _detect_harnesses():
        target = HARNESS_SKILL_DIRS[harness]
        if not (target / "SKILL.md").is_file():
            ok = False
            if not silent:
                click.secho(f"✗ {harness} skill missing: {target / 'SKILL.md'}", fg="red", err=True)
            continue
        stale_errors = _scan_stale_skill_patterns(target)
        if stale_errors:
            ok = False
            if not silent:
                click.secho(f"✗ {harness} skill contains stale update patterns:", fg="red", err=True)
                for error in stale_errors[:20]:
                    click.echo(f"  - {error}", err=True)
                if len(stale_errors) > 20:
                    click.echo(f"  ... {len(stale_errors) - 20} more", err=True)
        elif not silent:
            click.secho(f"✓ {harness} skill verified → {target}", fg="green")
    return ok


def _audit_update_state(
    *,
    expected_version: str | None,
    check_cli: bool,
    check_skills: bool,
    silent: bool,
) -> tuple[bool, str | None]:
    ok = True
    actual_version: str | None = None
    if check_cli:
        cli_ok, actual_version = _audit_global_cli(expected_version, silent)
        ok = cli_ok and ok
    if check_skills:
        ok = _audit_installed_skills(silent) and ok
    return ok, actual_version


def _print_status(check_result: dict, silent: bool) -> None:
    if silent:
        return
    latest = check_result.get("latest")
    current = check_result.get("current") or __version__
    if not latest:
        click.secho(
            f"! Couldn't reach upstream ({check_result.get('source')}); "
            "check your proxy / network.",
            fg="yellow",
            err=True,
        )
        return
    if _is_newer(latest, current):
        click.secho(
            f"⚠ InspireSkill v{latest} available (current v{current}).",
            fg="yellow",
        )
        click.echo("  run `inspire update` (no flags) to upgrade CLI + SKILL files in one go.")
    elif _is_newer(current, latest):
        click.secho(
            f"! Local InspireSkill v{current} is newer than published v{latest}.",
            fg="yellow",
        )
        click.echo("  global `inspire update` will install the latest published PyPI package.")
    else:
        click.secho(f"✓ InspireSkill is up to date (v{current}).", fg="green")


@click.command("update")
@click.option("--check", "check_only", is_flag=True, help="Only check upstream; don't upgrade.")
@click.option("--silent", is_flag=True, help="Suppress output (used by background checks).")
@click.option("--cli-only", is_flag=True, help="Upgrade the Python package and runtime only.")
@click.option("--skill-only", is_flag=True, help="Refresh SKILL.md + references/ only.")
def update(check_only: bool, silent: bool, cli_only: bool, skill_only: bool) -> None:
    """Check for and install newer InspireSkill versions."""
    if cli_only and skill_only:
        raise click.UsageError("--cli-only and --skill-only are mutually exclusive.")

    # --- check path -------------------------------------------------------
    if check_only:
        result = run_check(write=True)
        _print_status(result, silent)
        audit_ok, actual_version = _audit_update_state(
            expected_version=result.get("latest"),
            check_cli=True,
            check_skills=True,
            silent=silent,
        )
        if actual_version:
            run_check(write=True, current_version=actual_version)
        if not result.get("latest"):
            sys.exit(1)
        if not audit_ok:
            sys.exit(1)
        return

    # --- upgrade path -----------------------------------------------------
    # Always refresh the version cache first so subsequent invocations show
    # the correct state and the notice goes away if we successfully upgrade.
    pre = run_check(write=True)
    if not silent:
        _print_status(pre, silent=False)

    ok = True
    if not skill_only:
        ok = _upgrade_cli(silent, target_version=pre.get("latest")) and ok
        expected_version = str(pre.get("latest") or "")
        previous_version = str(pre.get("current") or __version__)
        if ok and expected_version and _is_newer(expected_version, __version__):
            if not _run_post_update_command(
                previous_version=previous_version,
                expected_version=expected_version,
                cli_only=cli_only,
                silent=silent,
            ):
                sys.exit(1)
            return
    if not cli_only:
        ok = _refresh_skill_files(silent, latest_version=pre.get("latest")) and ok

    # Verify the observable install state rather than trusting command exit
    # codes. This catches PATH shadowing, stale agent skill files, and local
    # uv-tool sources that would otherwise keep the global command outdated.
    audit_ok, actual_version = _audit_update_state(
        expected_version=pre.get("latest"),
        check_cli=not skill_only,
        check_skills=not cli_only,
        silent=silent,
    )
    ok = audit_ok and ok

    if ok and not skill_only:
        ok = _ensure_global_playwright_runtime(silent) and ok

    # Re-check after upgrade so the cache reflects the externally visible
    # PATH version, not the already-imported module version from this process.
    run_check(write=True, current_version=actual_version or __version__)

    if not ok:
        sys.exit(1)

    # Run environment normalization once after a successful upgrade so users
    # coming from v3.1.x (no sentinel yet) get pre-v3 unscoped files
    # quarantined and stale env vars flagged on the same `inspire update` they
    # ran to install v4. Idempotent via the normalization sentinel.
    _normalize_after_success(silent)

    if not silent:
        click.secho("✓ InspireSkill updated.", fg="green", bold=True)

    if not skill_only:
        previous_version = str(pre.get("current") or __version__)
        new_version = str(actual_version or pre.get("latest") or __version__)
        _print_release_summary(previous_version, new_version, silent=silent)
