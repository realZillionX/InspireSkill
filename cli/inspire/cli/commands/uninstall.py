"""`inspire uninstall` — remove InspireSkill from this machine.

Undoes what `scripts/install.sh` wrote, in three tiers:

    inspire uninstall                  # installer-owned files + the CLI package
    inspire uninstall --purge          # also ~/.inspire (accounts, sessions)
    inspire uninstall --purge-runtime  # also the shared Playwright browser cache

Design notes:
- The tiers are about ownership, not convenience. The default tier removes only
  what the installer created and nothing else wants: harness skill dirs, the
  launchd update-check agent, and the package. ``~/.inspire`` holds platform
  credentials that survive a reinstall, so dropping it takes ``--purge``. The
  Playwright browser cache sits in a *shared* location that any other Playwright
  user on this machine reads, so it takes ``--purge-runtime`` and is never
  implied by ``--purge``.
- ``INSPIRE.md`` is user documentation and is never uninstall output. Retired
  repository-local ``./.inspire/`` directories are handled by ``inspire update``
  stale-state sweeping rather than uninstall's installation inventory.
- The package comes off last, and only if every file removal succeeded — a
  half-cleaned machine should still have the command that finishes the job.
- ``scripts/install.sh --uninstall`` covers the same ground for a machine whose
  CLI no longer runs. The two share no code, so ``test_uninstall_command.py``
  pins the constants they both hard-code.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import click

from inspire.accounts.storage import inspire_home
from inspire.cli.commands.update import (
    HARNESS_SKILL_DIRS,
    _detect_installer,
    _log_completed_process,
    _pipx_tool_info,
    _uv_tool_info,
)
from inspire.cli.context import Context, EXIT_GENERAL_ERROR, pass_context
from inspire.cli.formatters import json_formatter
from inspire.cli.utils.errors import exit_with_error, require_confirmation
from inspire.cli.utils.update_notice import PACKAGE_NAME

logger = logging.getLogger(__name__)

# Kept in lockstep with LAUNCH_LABEL in scripts/install.sh.
LAUNCH_AGENT_LABEL = "sh.inspire-skill.update-check"
UPDATE_STATUS_FILENAME = "update-status.json"


@dataclass(frozen=True)
class Removal:
    """One path the uninstall will delete."""

    label: str
    path: Path


@dataclass(frozen=True)
class Kept:
    """One path deliberately left behind, and the flag that would remove it."""

    label: str
    path: Path
    flag: str
    reason: str


def _display_path(path: Path) -> str:
    """Render a path home-relative so output carries no local username.

    These paths are about to be deleted, so naming them is the whole point of
    the confirmation prompt — but ``~/.claude/skills/inspire`` says everything
    the absolute form does without the username riding along into whatever the
    user pastes into an issue.
    """
    try:
        # as_posix so the whole string reads as one path: a `~/` prefix spliced
        # onto Windows' backslashes gives `~/.claude\skills\inspire`.
        return f"~/{path.relative_to(Path.home()).as_posix()}"
    except ValueError:
        return str(path)


def _launch_agent_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def _launch_agent_log() -> Path:
    return Path.home() / "Library" / "Logs" / "inspire-skill-update-check.log"


def _playwright_cache_dir() -> Path | None:
    """Where Playwright keeps downloaded browsers, or None if it has no own dir.

    ``PLAYWRIGHT_BROWSERS_PATH=0`` means "inside the package", which the package
    removal already covers.
    """
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if override == "0":
        return None
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _skill_removals() -> list[Removal]:
    # `HARNESS_SKILL_DIRS` is built from `Path.home()` at import time, so a test
    # that fakes a home patches this module's binding, not `Path.home`.
    return [
        Removal(f"{harness} skill", target)
        for harness, target in sorted(HARNESS_SKILL_DIRS.items())
        if _exists(target)
    ]


def _plan(
    *, purge: bool, purge_runtime: bool
) -> tuple[list[Removal], list[Kept]]:
    removals = _skill_removals()
    kept: list[Kept] = []

    plist = _launch_agent_plist()
    if _exists(plist):
        removals.append(Removal("update-check agent", plist))
    log = _launch_agent_log()
    if _exists(log):
        removals.append(Removal("update-check log", log))

    home = inspire_home()
    if purge:
        if _exists(home):
            removals.append(Removal("Inspire home", home))
    else:
        status = home / UPDATE_STATUS_FILENAME
        if _exists(status):
            removals.append(Removal("update status", status))
        if _exists(home):
            kept.append(
                Kept(
                    "account config",
                    home,
                    "--purge",
                    "platform credentials and local caches, reusable after a reinstall",
                )
            )

    browsers = _playwright_cache_dir()
    if browsers is not None and _exists(browsers):
        if purge_runtime:
            removals.append(Removal("Playwright browsers", browsers))
        else:
            kept.append(
                Kept(
                    "Playwright browsers",
                    browsers,
                    "--purge-runtime",
                    "shared with every other Playwright user on this machine",
                )
            )

    return removals, kept


def _package_uninstall_command() -> tuple[list[str], str] | None:
    """The command that removes the installed package, plus its installer name.

    Mirrors ``update``'s resolution order: trust the installer that owns the
    running process, and otherwise fall back to whichever one reports a global
    ``inspire-skill``. Returns None when neither owns it — a source checkout or
    a plain ``pip install`` is the user's to unwind.
    """
    installer = _detect_installer()
    if installer == "uv":
        return ["uv", "tool", "uninstall", PACKAGE_NAME], "uv"
    if installer == "pipx":
        return ["pipx", "uninstall", PACKAGE_NAME], "pipx"
    if _uv_tool_info() is not None:
        logger.debug("Removing the discovered global uv tool installation")
        return ["uv", "tool", "uninstall", PACKAGE_NAME], "uv"
    if _pipx_tool_info() is not None:
        logger.debug("Removing the discovered global pipx installation")
        return ["pipx", "uninstall", PACKAGE_NAME], "pipx"
    logger.debug(
        "No supported global installer found: executable=%s prefix=%s",
        sys.executable,
        sys.prefix,
    )
    return None


def _unload_launch_agent() -> None:
    """Stop the update-check agent before its plist goes away.

    Deleting a loaded plist leaves launchd holding a job that points at a
    binary that no longer exists, which it then complains about on every login.
    """
    plist = _launch_agent_plist()
    if not _exists(plist):
        return
    cmd = ["launchctl", "unload", str(plist)]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as e:
        logger.debug("launchctl unload could not run: %s", e, exc_info=True)
        return
    _log_completed_process("launchctl unload", proc, cmd=cmd)


def _remove_path(path: Path) -> str | None:
    """Delete a file, directory, or symlink. Returns an error line on failure."""
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    except OSError as e:
        return f"{_display_path(path)}: {e.strerror or e}"
    return None


def _exit_now(code: int) -> None:
    """Leave immediately, skipping interpreter shutdown.

    By this point the venv this module was imported from is gone. Code already
    in memory keeps running — POSIX keeps an unlinked inode alive for the
    processes holding it — but any *later* import reads from a directory that
    no longer exists, and normal interpreter shutdown can import. So nothing
    may run after this call.
    """
    os._exit(code)


def _format_plan(removals: list[Removal], kept: list[Kept]) -> str:
    width = max(
        [len(item.label) for item in removals] + [len(item.label) for item in kept] + [0]
    )
    lines = ["About to remove:"]
    lines += [
        f"  {item.label.ljust(width)}  {_display_path(item.path)}" for item in removals
    ]
    if kept:
        lines.append("")
        lines.append("Keeping:")
        lines += [
            f"  {item.label.ljust(width)}  {_display_path(item.path)}"
            f"  ({item.reason}; pass {item.flag} to remove)"
            for item in kept
        ]
    return "\n".join(lines)


@click.command("uninstall")
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    help="Skip the interactive confirmation prompt.",
)
@click.option(
    "--purge",
    is_flag=True,
    help="Also remove the Inspire home directory (accounts, sessions, caches).",
)
@click.option(
    "--purge-runtime",
    is_flag=True,
    help="Also remove the Playwright browser cache, which other tools may share.",
)
@pass_context
def uninstall(
    ctx: Context,
    assume_yes: bool,
    purge: bool,
    purge_runtime: bool,
) -> None:
    """Remove InspireSkill from this machine.

    Removes the agent skills, the update-check agent, and the CLI package.
    Account config is kept unless --purge is passed; the shared Playwright
    browser cache is kept unless --purge-runtime is passed. User-authored
    INSPIRE.md files are never touched.

    \b
    Examples:
        inspire uninstall
        inspire uninstall --purge --yes
    """
    removals, kept = _plan(purge=purge, purge_runtime=purge_runtime)
    package = _package_uninstall_command()

    if not removals and package is None:
        if ctx.json_output:
            click.echo(
                json_formatter.format_json(
                    {"removed": [], "kept": [], "package": None, "uninstalled": False}
                )
            )
        else:
            click.secho(
                "Nothing to uninstall: no installed files or package found.", fg="green"
            )
        return

    if not ctx.json_output:
        if removals or kept:
            click.echo(_format_plan(removals, kept))
        if package is not None:
            click.echo(f"  CLI package: {PACKAGE_NAME} (via {package[1]})")
        elif removals:
            click.secho(
                "No uv or pipx installation found; the package itself stays.",
                fg="yellow",
            )
        click.echo()

    require_confirmation(
        ctx,
        yes=assume_yes,
        prompt="Remove InspireSkill from this machine?",
        message="Uninstall requires confirmation.",
        hint="Pass --yes to confirm removal.",
    )

    _unload_launch_agent()

    errors: list[str] = []
    removed: list[str] = []
    for item in removals:
        error = _remove_path(item.path)
        if error:
            errors.append(error)
            continue
        removed.append(item.label)
        logger.debug("Removed %s at %s", item.label, item.path)

    if errors:
        # Leave the package installed so `inspire uninstall` can be retried
        # once the user has cleared whatever blocked the deletion.
        exit_with_error(
            ctx,
            "UninstallError",
            "Some files could not be removed; the CLI package was left installed.",
            EXIT_GENERAL_ERROR,
            hint="; ".join(errors),
        )
        return

    kept_labels = [item.label for item in kept]
    if package is None:
        click.echo(
            _render_result(
                ctx,
                removed=removed,
                kept=kept_labels,
                installer=None,
                package_removed=False,
            )
        )
        return

    # Everything below runs against a venv that is about to vanish. Render both
    # outcomes now, while imports still work, then pick one and leave.
    cmd, installer = package
    success = _render_result(
        ctx, removed=[*removed, "CLI package"], kept=kept_labels,
        installer=installer, package_removed=True,
    )
    failure = _render_result(
        ctx, removed=removed, kept=kept_labels,
        installer=installer, package_removed=False,
        error=f"`{' '.join(cmd)}` failed; remove the package manually.",
    )

    try:
        proc = subprocess.run(
            cmd,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as e:
        logger.debug("Package uninstall could not run: %s", e, exc_info=True)
        click.echo(failure, err=True)
        _exit_now(EXIT_GENERAL_ERROR)
        return

    _log_completed_process("package uninstall", proc, cmd=cmd)
    if proc.returncode != 0:
        click.echo(failure, err=True)
        _exit_now(EXIT_GENERAL_ERROR)
        return

    click.echo(success)
    _exit_now(0)


def _render_result(
    ctx: Context,
    *,
    removed: list[str],
    kept: list[str],
    installer: str | None,
    package_removed: bool,
    error: str | None = None,
) -> str:
    """Pre-render the closing report so nothing has to format it post-teardown."""
    if ctx.json_output:
        payload: dict[str, object] = {
            "removed": removed,
            "kept": kept,
            "package": (
                {"name": PACKAGE_NAME, "installer": installer, "removed": package_removed}
                if installer
                else None
            ),
            "uninstalled": package_removed,
        }
        if error:
            payload["error"] = error
        return json_formatter.format_json(payload)

    lines: list[str] = []
    if error:
        lines.append(f"✗ {error}")
    lines.append(
        "InspireSkill uninstalled." if package_removed else "InspireSkill files removed."
    )
    if removed:
        lines.append(f"Removed: {', '.join(removed)}.")
    if kept:
        lines.append(f"Kept: {', '.join(kept)}.")
    return "\n".join(lines)
