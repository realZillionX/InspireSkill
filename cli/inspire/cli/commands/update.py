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
- This command is read by people, not agents: it reports each stage as it runs,
  names the harnesses whose skill was refreshed, and summarizes what changed
  between the old and new version from GitHub Releases (CHANGELOG.md on main is
  the fallback). Everything diagnostic goes to `--debug` logs instead.
- The Python package is upgraded via whatever installer currently owns it
  (`uv tool upgrade` / `pipx upgrade`), detected from ``sys.executable``'s
  path. ``inspire-skill`` is published to PyPI, so the standard upgrade path
  works — the `install.sh` default SPEC is also the PyPI package name, so
  first-time install and `inspire update` pull from the same source.
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import re
import shutil
import shlex
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
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
from inspire.cli.context import Context, EXIT_GENERAL_ERROR
from inspire.cli.formatters import json_formatter
from inspire.accounts.normalize import (
    _install_playwright_chromium,
    _playwright_chromium_available,
)

logger = logging.getLogger(__name__)


def _opencode_config_dir() -> Path:
    """Resolve OpenCode's config dir: $OPENCODE_CONFIG_DIR or ~/.config/opencode."""
    override = os.environ.get("OPENCODE_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "opencode"


def _kimi_code_home() -> Path:
    """Resolve Kimi Code's home dir: $KIMI_CODE_HOME or ~/.kimi-code."""
    override = os.environ.get("KIMI_CODE_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".kimi-code"


def _kimi_desktop_root() -> Path:
    """Resolve Kimi Desktop's macOS daemon data root."""
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "kimi-desktop"
        / "daimon-share"
        / "daimon"
    )


HARNESS_SKILL_DIRS = {
    "claude": Path.home() / ".claude" / "skills" / "inspire",
    "codex": Path.home() / ".codex" / "skills" / "inspire",
    "antigravity": Path.home() / ".gemini" / "config" / "skills" / "inspire",
    "cursor": Path.home() / ".cursor" / "skills" / "inspire",
    "openclaw": Path.home() / ".openclaw" / "skills" / "inspire",
    "opencode": _opencode_config_dir() / "skills" / "inspire",
    "qoder": Path.home() / ".qoder" / "skills" / "inspire",
    "qoder-work": Path.home() / ".qoderwork" / "skills" / "inspire",
    "kimi-code": _kimi_code_home() / "skills" / "inspire",
    "kimi-desktop": _kimi_desktop_root() / "skills" / "inspire",
}
HARNESS_ROOTS = {
    "claude": Path.home() / ".claude",
    "codex": Path.home() / ".codex",
    "antigravity": Path.home() / ".gemini",
    "cursor": Path.home() / ".cursor",
    "openclaw": Path.home() / ".openclaw",
    "opencode": _opencode_config_dir(),
    "qoder": Path.home() / ".qoder",
    "qoder-work": Path.home() / ".qoderwork",
    "kimi-code": _kimi_code_home(),
    "kimi-desktop": _kimi_desktop_root(),
}

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
    r"^#{1,2}\s+(?P<tag>v?\d+(?:\.\d+){1,3}(?:[A-Za-z0-9._+-]*)?)\s*$",
    re.MULTILINE,
)
_RELEASE_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?P<text>.+?)\s*$")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_RAW_URL_RE = re.compile(r"https?://\S+")
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w.])/(?:[^\s`/]+/)*[^\s`/]+")
_RELEASE_SUMMARY_MAX_RELEASES = 3
_RELEASE_SUMMARY_MAX_ITEMS = 6
_RELEASE_SUMMARY_ITEM_MAX_CHARS = 160
# The summary is for users, not maintainers: drop entries that only describe
# how the release was built or installed.
_RELEASE_ENGINEERING_HINTS = (
    "uv tool ",
    "pipx ",
    "playwright install",
    "python -m ",
    "curl ",
    "pypi",
    "http_proxy",
    "https_proxy",
)
_DEBUG_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_DEBUG_SECRET_OPTION_RE = re.compile(
    r"(?i)(?P<prefix>--(?:access[-_]?token|refresh[-_]?token|token|password|passwd|"
    r"api[-_]?key|apikey)(?:=|\s+))(?P<value>\S+)"
)
_DEBUG_SECRET_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    (?P<prefix>
        (?<![\w-])
        ["']?
        (?:access[-_]?token|refresh[-_]?token|token|password|passwd|api[-_]?key|apikey)
        ["']?
        \s*[:=]\s*
    )
    (?P<value>"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;&]+)
    """
)
_DEBUG_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![:/\w.])/(?:[^\s`/]+/)*[^\s`/]+"
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


def _current_output_context() -> Context:
    click_ctx = click.get_current_context(silent=True)
    if click_ctx is not None:
        shared = click_ctx.find_object(Context)
        if shared is not None:
            return shared
    return Context()


def _emit_stage(message: str, *, silent: bool) -> None:
    """Print one progress line for a step that can take a while.

    Suppressed under ``--json`` so progress never lands in the payload.
    """
    if silent or _current_output_context().json_output:
        return
    click.secho(f"› {message}", fg="blue")


def _emit_update_failure(*, silent: bool, check_only: bool = False) -> None:
    if silent:
        return
    action = "check" if check_only else "update"
    ctx = _current_output_context()
    if ctx.json_output:
        click.echo(
            json_formatter.format_json_error(
                "UpdateError",
                f"InspireSkill {action} failed.",
                EXIT_GENERAL_ERROR,
                hint=(
                    f"Retry with `inspire --debug update"
                    f"{' --check' if check_only else ''}` for diagnostics."
                ),
            ),
            err=True,
        )
        return
    click.secho(
        f"✗ InspireSkill {action} failed. Retry with `inspire --debug update"
        f"{' --check' if check_only else ''}` for diagnostics.",
        fg="red",
        err=True,
    )


def _scrub_debug_text(value: str) -> str:
    def _scrub_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        trailing = ""
        while raw and raw[-1] in ".,;)]}":
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        try:
            parsed = urllib.parse.urlsplit(raw)
            hostname = parsed.hostname
            if not hostname:
                return "<url>" + trailing
            host = f"[{hostname}]" if ":" in hostname else hostname
            try:
                port = parsed.port
            except ValueError:
                port = None
            netloc = f"{host}:{port}" if port is not None else host
            query = "<redacted>" if parsed.query else ""
            fragment = "<redacted>" if parsed.fragment else ""
            scrubbed = urllib.parse.urlunsplit(
                (parsed.scheme, netloc, parsed.path, query, fragment)
            )
            return scrubbed + trailing
        except ValueError:
            return "<url>" + trailing

    scrubbed = _DEBUG_URL_RE.sub(_scrub_url, str(value or ""))
    scrubbed = _DEBUG_SECRET_OPTION_RE.sub(
        lambda match: f"{match.group('prefix')}<redacted>",
        scrubbed,
    )
    scrubbed = _DEBUG_SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('prefix')}<redacted>",
        scrubbed,
    )
    return _DEBUG_ABSOLUTE_PATH_RE.sub("<path>", scrubbed)


def _log_completed_process(
    label: str,
    proc: subprocess.CompletedProcess[str],
    *,
    cmd: list[str] | None = None,
) -> None:
    logger.debug(
        "%s command=%s exit=%s stdout=%r stderr=%r",
        label,
        _scrub_debug_text(shlex.join(cmd)) if cmd else "<internal>",
        proc.returncode,
        _scrub_debug_text(proc.stdout or ""),
        _scrub_debug_text(proc.stderr or ""),
    )


@contextlib.contextmanager
def _suppress_subprocess_output() -> Iterator[None]:
    """Capture child-process output for debug logs without printing it."""
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)
    with (
        tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout,
        tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr,
    ):
        try:
            os.dup2(stdout.fileno(), 1)
            os.dup2(stderr.fileno(), 2)
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                yield
        finally:
            os.dup2(saved_stdout, 1)
            os.dup2(saved_stderr, 2)
            os.close(saved_stdout)
            os.close(saved_stderr)
            stdout.seek(0)
            stderr.seek(0)
            logger.debug(
                "Suppressed child output: stdout=%r stderr=%r",
                _scrub_debug_text(stdout.read()),
                _scrub_debug_text(stderr.read()),
            )


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
    del silent  # Output is always captured; --debug receives it through logging.
    proc = subprocess.run(
        cmd,
        check=False,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    _log_completed_process("package upgrade", proc, cmd=cmd)
    return proc.returncode, output


def _upgrade_cli(silent: bool, target_version: str | None = None) -> bool:
    installer = _detect_installer()
    uv_info = None if silent and installer in {"uv", "pipx"} else _uv_tool_info()
    if installer == "uv":
        cmd = _official_uv_install_cmd(target_version)
        if uv_info and _is_local_requirement(uv_info.required):
            logger.debug(
                "Resetting local uv requirement: %s",
                _scrub_debug_text(uv_info.required or ""),
            )
    elif installer == "pipx":
        cmd = ["pipx", "upgrade", PACKAGE_NAME]
    elif uv_info is not None:
        cmd = _official_uv_install_cmd(target_version)
        logger.debug("Updating the discovered global uv tool installation")
        if _is_local_requirement(uv_info.required):
            logger.debug(
                "Resetting local uv requirement: %s",
                _scrub_debug_text(uv_info.required or ""),
            )
    elif _pipx_tool_info() is not None:
        cmd = ["pipx", "upgrade", PACKAGE_NAME]
        logger.debug("Updating the discovered global pipx installation")
    else:
        logger.debug(
            "No supported global installer found: executable=%s prefix=%s",
            sys.executable,
            sys.prefix,
        )
        return False

    logger.debug(
        "Selected installer=%s command=%s",
        installer or "discovered",
        _scrub_debug_text(shlex.join(cmd)),
    )
    try:
        returncode, output = _run_upgrade_command(cmd, silent=silent)
    except FileNotFoundError as exc:
        logger.debug("Package installer is unavailable: %s", exc, exc_info=True)
        return False

    if returncode == 0:
        return True

    if _is_likely_network_or_index_error(output):
        for index_url in PYPI_MIRROR_INDEX_URLS:
            logger.debug("Retrying package upgrade with mirror=%s", index_url)
            try:
                retry_code, retry_output = _run_upgrade_command(
                    cmd,
                    silent=silent,
                    env=_upgrade_env_with_index(index_url),
                )
            except FileNotFoundError as exc:
                logger.debug("Package installer disappeared during retry: %s", exc, exc_info=True)
                return False
            if retry_code == 0:
                return True
            output += "\n" + retry_output

        logger.debug(
            "Package upgrade failed after all mirror retries: %s",
            _scrub_debug_text(output),
        )
        return False

    logger.debug(
        "Package upgrade failed with exit=%s output=%s",
        returncode,
        _scrub_debug_text(output),
    )
    return False


def _ensure_playwright_runtime(silent: bool) -> bool:
    """Ensure the installed InspireSkill environment can launch Chromium."""
    del silent
    if _playwright_chromium_available():
        logger.debug("Playwright Chromium runtime is ready")
        return True

    logger.debug("Installing Playwright Chromium runtime")
    with _suppress_subprocess_output():
        installed = _install_playwright_chromium(include_system_deps=None)
    if not installed:
        logger.debug("Playwright Chromium runtime installation failed")
        return False

    if _playwright_chromium_available():
        logger.debug("Playwright Chromium runtime verified after installation")
        return True

    logger.debug("Playwright Chromium installed but launch probe failed")
    return False


def _global_inspire_executable() -> str | None:
    uv_info = _uv_tool_info()
    if uv_info and uv_info.executable_path:
        return uv_info.executable_path
    return shutil.which("inspire")


def _ensure_global_playwright_runtime(silent: bool) -> bool:
    executable = _global_inspire_executable()
    if not executable:
        logger.debug("Global inspire executable is unavailable for runtime setup")
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
        logger.debug("Runtime setup could not start: executable=%s error=%s", executable, e)
        return False
    _log_completed_process("global runtime setup", proc, cmd=cmd)
    if proc.returncode == 0:
        return True

    return False


def _run_post_update_command(
    *,
    expected_version: str,
    cli_only: bool,
    silent: bool,
) -> bool:
    executable = _global_inspire_executable()
    if not executable:
        logger.debug("Global inspire executable is unavailable for post-update setup")
        return False

    cmd = [
        executable,
        "_post-update",
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
        logger.debug("Post-update setup could not start: executable=%s error=%s", executable, e)
        return False
    _log_completed_process("post-update setup", proc, cmd=cmd)
    return proc.returncode == 0


def _download_tarball(timeout: int = 30) -> bytes | None:
    req = urllib.request.Request(
        TARBALL_URL,
        headers={"User-Agent": f"inspire-skill/{__version__}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        logger.debug("Skill tarball fetch failed: %s", e, exc_info=True)
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
      Python 3.10 fallback (``extractall`` without a filter
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
                logger.debug("Skill tarball has unexpected top-level entries: %s", top_segments)
                return None
            top = top_segments.pop()
            try:
                tf.extractall(dest, filter="data")
            except TypeError:
                # Python < 3.11.4 (no `filter=` kwarg). codeload is GitHub
                # which we trust, so the fallback extract is acceptable.
                tf.extractall(dest)
            extracted = dest / top
            return extracted if extracted.is_dir() else None
    except (tarfile.TarError, OSError) as e:
        logger.debug("Skill tarball extraction failed: %s", e, exc_info=True)
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
    return errors


def _refresh_skill_files(silent: bool) -> bool:
    del silent
    harnesses = _detect_harnesses()
    if not harnesses:
        logger.debug("No supported agent harness detected; skipping skill refresh")
        return True  # not a failure; user may run skill-less

    tarball = _download_tarball()
    if tarball is None:
        return False

    with tempfile.TemporaryDirectory(prefix="inspire-skill-") as tmp:
        extracted = _extract_assets(tarball, Path(tmp))
        if extracted is None:
            logger.debug("Skill tarball has no usable top-level directory")
            return False

        src_skill = extracted / "SKILL.md"
        src_refs = extracted / "references"
        if not src_skill.is_file():
            logger.debug("SKILL.md is missing from the skill tarball")
            return False

        for harness in harnesses:
            target = HARNESS_SKILL_DIRS[harness]
            # Wipe any previous install, including stale symlinks or files.
            if target.exists() or target.is_symlink():
                try:
                    if target.is_symlink() or target.is_file():
                        target.unlink()
                    else:
                        shutil.rmtree(target)
                except OSError as e:
                    logger.debug("Could not clean skill target %s: %s", target, e, exc_info=True)
                    return False
            target.mkdir(parents=True, exist_ok=True)

            shutil.copy2(src_skill, target / "SKILL.md")
            if src_refs.is_dir():
                shutil.copytree(src_refs, target / "references", dirs_exist_ok=True)

            verify_errors = _verify_skill_target(extracted, target)
            if verify_errors:
                logger.debug(
                    "Skill verification failed for %s: %s",
                    target,
                    verify_errors,
                )
                return False

            if harness == "codex":
                agents_dir = target / "agents"
                agents_dir.mkdir(parents=True, exist_ok=True)
                (agents_dir / "openai.yaml").write_text(
                    'interface:\n'
                    '  display_name: "Inspire"\n'
                    '  short_description: "Operate Inspire with focused references and live platform data."\n'
                    '  default_prompt: "Use $inspire to plan and execute this Inspire platform task safely."\n',
                    encoding="utf-8",
                )

            logger.debug("Refreshed %s skill at %s", harness, target)

    return True


def _installed_skill_harnesses() -> list[str]:
    """Harnesses that currently hold an installed skill.

    Read from the filesystem rather than from ``_refresh_skill_files``'s own
    bookkeeping, because on a self-upgrade the refresh runs inside the newly
    installed CLI and its output never reaches the process printing the
    summary.
    """
    return [
        harness
        for harness in _detect_harnesses()
        if (HARNESS_SKILL_DIRS[harness] / "SKILL.md").is_file()
    ]


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
    tarball = _download_tarball(timeout=timeout)
    if tarball is None:
        return []
    text = _changelog_text_from_tarball(tarball)
    if text is None:
        return []
    return _release_entries_from_changelog_text(text)


def _fetch_release_entries(timeout: int = 10) -> list[ReleaseEntry]:
    """Release bodies, preferring GitHub Releases and falling back to CHANGELOG.md.

    Releases can lag a freshly published package by a few minutes, and the API
    is rate-limited for unauthenticated callers; the CHANGELOG on ``main`` is
    always there.
    """
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


def _compact_release_item(text: str) -> str:
    text = _MARKDOWN_LINK_RE.sub(r"\1", text)
    text = _RAW_URL_RE.sub("", text)
    text = _ABSOLUTE_PATH_RE.sub("<path>", text)
    text = re.sub(r"\s+", " ", text).strip(" -:")
    if len(text) > _RELEASE_SUMMARY_ITEM_MAX_CHARS:
        text = text[: _RELEASE_SUMMARY_ITEM_MAX_CHARS - 1].rstrip() + "…"
    return text


def _unwrap_release_lines(body: str) -> list[str]:
    """Fold hard-wrapped continuation lines back into their bullet.

    Release bodies wrap at roughly 90 columns, so the tail of a bullet arrives
    as an indented continuation line. Read line by line, every such bullet
    would be reported cut off mid-sentence.
    """
    lines: list[str] = []
    for raw in body.splitlines():
        stripped = raw.strip()
        is_continuation = (
            bool(lines)
            and bool(stripped)
            and raw[:1].isspace()
            and not stripped.startswith(("#", "```", ">"))
            and not _RELEASE_BULLET_RE.match(raw)
            and bool(_RELEASE_BULLET_RE.match(lines[-1]))
        )
        if is_continuation:
            lines[-1] = f"{lines[-1].rstrip()} {stripped}"
        else:
            lines.append(raw)
    return lines


def _release_items_for_display(body: str) -> list[str]:
    lines = _unwrap_release_lines(_release_body_for_display(body))
    items: list[str] = []
    for line in lines:
        match = _RELEASE_BULLET_RE.match(line)
        if not match:
            continue
        item = _compact_release_item(match.group("text"))
        if any(hint in item.lower() for hint in _RELEASE_ENGINEERING_HINTS):
            continue
        if item and item not in items:
            items.append(item)
    if items:
        return items

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "```", ">")):
            continue
        item = _compact_release_item(stripped)
        if any(hint in item.lower() for hint in _RELEASE_ENGINEERING_HINTS):
            continue
        return [item] if item else []
    return []


def _release_summary_items(previous_version: str, new_version: str) -> list[tuple[str, str]]:
    """``(version, item)`` pairs describing what changed between two versions."""
    if not _is_newer(new_version, previous_version):
        return []

    entries = _release_entries_between(
        _fetch_release_entries(),
        previous_version=previous_version,
        new_version=new_version,
    )
    if not entries:
        logger.debug(
            "No release summary available between v%s and v%s",
            previous_version,
            new_version,
        )
        return []

    displayed: list[tuple[str, str]] = []
    for entry in entries[:_RELEASE_SUMMARY_MAX_RELEASES]:
        version = _normalize_release_version(entry.tag)
        for item in _release_items_for_display(entry.body):
            displayed.append((version, item))
            if len(displayed) >= _RELEASE_SUMMARY_MAX_ITEMS:
                break
        if len(displayed) >= _RELEASE_SUMMARY_MAX_ITEMS:
            break

    if not displayed:
        logger.debug("Release entries contained no compact user-facing summary")
    return displayed


def _run_post_update_tasks(
    *,
    expected_version: str | None,
    cli_only: bool,
    silent: bool,
) -> bool:
    ok = True
    if not cli_only:
        ok = _refresh_skill_files(silent) and ok

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
    del silent
    uv_info = _uv_tool_info()
    executable_hint = uv_info.executable_path if uv_info and uv_info.executable_path else None
    executable, actual_version, detail = _read_inspire_version(executable_hint)
    ok = True
    if executable is None:
        ok = False
        logger.debug("Global inspire executable is not on PATH after update")
    elif actual_version is None:
        ok = False
        logger.debug(
            "Could not parse updated InspireSkill version: executable=%s detail=%s",
            executable,
            detail,
        )
    elif expected_version and _is_newer(expected_version, actual_version):
        ok = False
        logger.debug(
            "Updated executable is stale: executable=%s actual=%s expected=%s",
            executable,
            actual_version,
            expected_version,
        )
    else:
        logger.debug("Updated executable verified: executable=%s version=%s", executable, actual_version)

    if uv_info and _is_local_requirement(uv_info.required):
        ok = False
        logger.debug("Global uv tool still points at local source: %s", uv_info.required)
    return ok, actual_version


def _audit_installed_skills(silent: bool) -> bool:
    del silent
    ok = True
    for harness in _detect_harnesses():
        target = HARNESS_SKILL_DIRS[harness]
        if not (target / "SKILL.md").is_file():
            ok = False
            logger.debug("%s skill is missing from %s", harness, target)
            continue
        logger.debug("%s skill verified at %s", harness, target)
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
        logger.debug("Update check did not return a latest version: %s", check_result)
        return
    if _is_newer(latest, current):
        click.secho(
            f"Update available: v{current} → v{latest}.",
            fg="yellow",
        )
    elif _is_newer(current, latest):
        click.secho(
            f"Local version v{current}; published version v{latest}.",
            fg="yellow",
        )
    else:
        click.secho(f"InspireSkill is up to date (v{current}).", fg="green")


def _emit_update_success(
    version: str,
    *,
    previous_version: str | None = None,
    report_skills: bool = False,
    silent: bool,
    state_sweep: dict[str, object] | None = None,
) -> None:
    """Report a completed update: version, refreshed harnesses, and what's new.

    ``report_skills`` is off for ``--cli-only``, where no harness skill was
    touched. Harnesses are named, never pathed — local paths stay out of
    public output.
    """
    if silent:
        return
    harnesses = _installed_skill_harnesses() if report_skills else []
    notes = (
        _release_summary_items(previous_version, version)
        if previous_version
        else []
    )

    ctx = _current_output_context()
    if ctx.json_output:
        payload: dict[str, object] = {"version": version, "updated": True}
        if report_skills:
            payload["skills"] = harnesses
        if notes:
            payload["release_notes"] = [
                {"version": note_version, "summary": item} for note_version, item in notes
            ]
        if state_sweep:
            payload["stale_state"] = state_sweep
        click.echo(json_formatter.format_json(payload))
        return

    click.secho(f"InspireSkill updated to v{version}.", fg="green", bold=True)
    if report_skills:
        if harnesses:
            click.secho(f"Skills refreshed: {', '.join(harnesses)}.", fg="green")
        else:
            click.secho(
                "No agent harness detected, so no skill was installed.",
                fg="yellow",
            )
    if notes:
        click.secho(f"What's new (v{previous_version} → v{version}):", bold=True)
        for note_version, item in notes:
            click.echo(f"- v{note_version}: {item}")


def _emit_update_check(result: dict, *, actual_version: str | None, silent: bool) -> None:
    if silent:
        return
    latest = result.get("latest")
    current = actual_version or result.get("current") or __version__
    if not isinstance(current, str):
        current = str(current)
    if not isinstance(latest, str) or not latest:
        _emit_update_failure(silent=False, check_only=True)
        return
    ctx = _current_output_context()
    if ctx.json_output:
        click.echo(
            json_formatter.format_json(
                {
                    "current": current,
                    "latest": latest,
                    "update_available": _is_newer(latest, current),
                }
            )
        )
        return
    _print_status(
        {"current": current, "latest": latest},
        silent=False,
    )


def _sweep_orphan_state(
    *,
    silent: bool,
    assume_yes: bool,
    json_output: bool,
) -> dict[str, object] | None:
    """Offer to delete state files no current code path reads.

    A release that stops using a state file cannot delete it on the way out —
    the code that knew about it is what got removed. So the sweep runs here,
    where a version transition is already happening, comparing what is on disk
    against `state_inventory`'s declaration of what this version owns.

    Never deletes without consent and never prompts where no one can answer:
    silent mode is the background update check, and JSON mode has no operator,
    so both report through the return value and delete only under ``--yes``.

    Imported here rather than at module scope on purpose: by the time this
    runs, the package on disk is already the new version, so this picks up the
    incoming release's manifest instead of the running process's. That is the
    manifest we want — it knows what the new version stopped using — but it
    means loading a module this process was not built against, so every failure
    below is swallowed. Sweeping is housekeeping; it must never turn a
    successful upgrade into a traceback. The next `inspire update` retries.
    """
    try:
        from inspire.accounts.state_inventory import find_orphan_state

        orphans = find_orphan_state()
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.debug("Orphan state scan failed: %s", exc)
        return None
    if not orphans:
        return None

    if silent:
        logger.debug("Skipping orphan sweep in silent mode: %d found", len(orphans))
        return None

    found = [f"{o.display}{'/' if o.is_dir else ''}" for o in orphans]

    if not json_output:
        click.echo()
        click.echo("Local state left behind by older versions:")
        for item in found:
            click.echo(f"  {item}")

    if not assume_yes:
        hint = "Re-run `inspire update --yes` to delete them."
        if json_output:
            return {"found": found, "removed": 0}
        if not sys.stdin.isatty():
            click.echo(hint)
            return {"found": found, "removed": 0}
        if not click.confirm("\nDelete them?", default=False):
            click.echo(f"Kept. {hint}")
            return {"found": found, "removed": 0}

    removed = 0
    for orphan in orphans:
        try:
            if orphan.is_dir:
                shutil.rmtree(orphan.path)
            else:
                orphan.path.unlink()
        except OSError as exc:
            if not json_output:
                click.echo(f"Could not remove {orphan.display}: {exc}", err=True)
            continue
        removed += 1
    if not json_output:
        click.echo(f"Removed {removed} stale item{'' if removed == 1 else 's'}.")
    return {"found": found, "removed": removed}


@click.command("update")
@click.option("--check", "check_only", is_flag=True, help="Only check upstream; don't upgrade.")
@click.option("--silent", is_flag=True, help="Suppress output (used by background checks).")
@click.option("--cli-only", is_flag=True, help="Upgrade the Python package and runtime only.")
@click.option("--skill-only", is_flag=True, help="Refresh SKILL.md + references/ only.")
@click.option(
    "-y",
    "--yes",
    "assume_yes",
    is_flag=True,
    help="Delete state left by older versions without asking.",
)
def update(
    check_only: bool,
    silent: bool,
    cli_only: bool,
    skill_only: bool,
    assume_yes: bool,
) -> None:
    """Check for and install newer InspireSkill versions.

    Also sweeps local state that older versions wrote and no current code path
    reads. The sweep runs on every upgrade, including one that finds nothing
    new to install, so it doubles as the way to run it on demand.
    """
    if cli_only and skill_only:
        raise click.UsageError("--cli-only and --skill-only are mutually exclusive.")

    # --- check path -------------------------------------------------------
    if check_only:
        _emit_stage("Checking for updates...", silent=silent)
        result = run_check(write=True)
        # `expected_version` stays None here on purpose. The audit's version
        # comparison is a *post-upgrade* verifier ("did the executable actually
        # become the version we just installed?"), so feeding it the latest
        # published version makes "an update is available" — the one thing this
        # command exists to report — come back as `check failed`. What the audit
        # still earns its keep for is a broken install: executable not on PATH,
        # unparseable version, a global uv tool pinned to a local source, or a
        # detected harness with no SKILL.md.
        audit_ok, actual_version = _audit_update_state(
            expected_version=None,
            check_cli=True,
            check_skills=True,
            silent=silent,
        )
        if actual_version:
            run_check(write=True, current_version=actual_version)
        if not result.get("latest"):
            _emit_update_failure(silent=silent, check_only=True)
            sys.exit(1)
        if not audit_ok:
            _emit_update_failure(silent=silent, check_only=True)
            sys.exit(1)
        _emit_update_check(result, actual_version=actual_version, silent=silent)
        return

    # --- upgrade path -----------------------------------------------------
    # Always refresh the version cache first so subsequent invocations show
    # the correct state and the notice goes away if we successfully upgrade.
    _emit_stage("Checking for updates...", silent=silent)
    pre = run_check(write=True)
    logger.debug("Pre-update status: %s", pre)
    previous_version = str(pre.get("current") or __version__)

    ok = True
    if not skill_only:
        _emit_stage("Updating CLI...", silent=silent)
        ok = _upgrade_cli(silent, target_version=pre.get("latest")) and ok
        expected_version = str(pre.get("latest") or "")
        if ok and expected_version and _is_newer(expected_version, __version__):
            _emit_stage("Completing setup...", silent=silent)
            if not _run_post_update_command(
                expected_version=expected_version,
                cli_only=cli_only,
                silent=silent,
            ):
                _emit_update_failure(silent=silent)
                sys.exit(1)
            sweep = _sweep_orphan_state(
                silent=silent,
                assume_yes=assume_yes,
                json_output=_current_output_context().json_output,
            )
            _emit_update_success(
                expected_version,
                previous_version=previous_version,
                report_skills=not cli_only,
                silent=silent,
                state_sweep=sweep,
            )
            return
    if not cli_only:
        _emit_stage("Refreshing agent skills...", silent=silent)
        ok = _refresh_skill_files(silent) and ok

    # Verify the observable install state rather than trusting command exit
    # codes. This catches PATH shadowing, stale agent skill files, and local
    # uv-tool sources that would otherwise keep the global command outdated.
    _emit_stage("Verifying installation...", silent=silent)
    audit_ok, actual_version = _audit_update_state(
        expected_version=pre.get("latest"),
        check_cli=not skill_only,
        check_skills=not cli_only,
        silent=silent,
    )
    ok = audit_ok and ok

    if ok and not skill_only:
        _emit_stage("Preparing browser runtime...", silent=silent)
        ok = _ensure_global_playwright_runtime(silent) and ok

    # Re-check after upgrade so the cache reflects the externally visible
    # PATH version, not the already-imported module version from this process.
    run_check(write=True, current_version=actual_version or __version__)

    if not ok:
        _emit_update_failure(silent=silent)
        sys.exit(1)

    final_version = str(actual_version or pre.get("latest") or __version__)
    sweep = _sweep_orphan_state(
        silent=silent,
        assume_yes=assume_yes,
        json_output=_current_output_context().json_output,
    )
    _emit_update_success(
        final_version,
        # --skill-only never moves the CLI version, so there is nothing new to
        # summarize; the release notes describe package releases.
        previous_version=None if skill_only else previous_version,
        report_skills=not cli_only,
        silent=silent,
        state_sweep=sweep,
    )
