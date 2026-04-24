"""`inspire update` — check for and install newer InspireSkill versions.

Covers two things a user might want:

    inspire update                 # full upgrade: CLI package + SKILL/references
    inspire update --check         # only check upstream; write cache; print status
    inspire update --silent        # suppress output (used by the background check)
    inspire update --cli-only      # upgrade the Python package only
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
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


def _opencode_config_dir() -> Path:
    """Resolve OpenCode's config dir: $OPENCODE_CONFIG_DIR or ~/.config/opencode."""
    override = os.environ.get("OPENCODE_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "opencode"

import click

from inspire import __version__
from inspire.cli.utils.update_notice import (
    PACKAGE_NAME,
    TARBALL_URL,
    fetch_latest_version,
    run_check,
    _is_newer,
    _read_cache,
)

HARNESS_SKILL_DIRS = {
    "claude":   Path.home() / ".claude"   / "skills" / "inspire",
    "codex":    Path.home() / ".codex"    / "skills" / "inspire",
    "gemini":   Path.home() / ".gemini"   / "skills" / "inspire",
    "openclaw": Path.home() / ".openclaw" / "skills" / "inspire",
    "opencode": _opencode_config_dir()    / "skills" / "inspire",
}
HARNESS_ROOTS = {
    "claude":   Path.home() / ".claude",
    "codex":    Path.home() / ".codex",
    "gemini":   Path.home() / ".gemini",
    "openclaw": Path.home() / ".openclaw",
    "opencode": _opencode_config_dir(),
}

SKILL_ASSETS = ("SKILL.md", "references")


def _detect_harnesses() -> list[str]:
    return [h for h, root in HARNESS_ROOTS.items() if root.is_dir()]


def _detect_installer() -> str | None:
    """Guess which installer owns the current `inspire` process.

    Returns "uv", "pipx", or None (editable / unknown).
    """
    exe = Path(sys.executable).resolve()
    parts = exe.parts
    if "uv" in parts and "tools" in parts:
        return "uv"
    if "pipx" in parts and "venvs" in parts:
        return "pipx"
    return None


def _upgrade_cli(silent: bool) -> bool:
    installer = _detect_installer()
    if installer == "uv":
        cmd = ["uv", "tool", "upgrade", PACKAGE_NAME]
    elif installer == "pipx":
        cmd = ["pipx", "upgrade", PACKAGE_NAME]
    else:
        if not silent:
            click.secho(
                "✗ Can't auto-upgrade: this build isn't managed by uv tool / pipx "
                f"(python={sys.executable}). Reinstall via scripts/install.sh or rerun "
                "./scripts/install-dev.sh from a local clone.",
                fg="red",
                err=True,
            )
        return False

    if not silent:
        click.secho(f"› {' '.join(cmd)}", fg="blue")
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        if not silent:
            click.secho(f"✗ `{cmd[0]}` not on PATH.", fg="red", err=True)
        return False
    except subprocess.CalledProcessError as e:
        if not silent:
            click.secho(f"✗ {cmd[0]} upgrade failed: exit {e.returncode}", fg="red", err=True)
        return False
    return True


def _download_tarball(timeout: int = 30) -> bytes | None:
    req = urllib.request.Request(
        TARBALL_URL,
        headers={"User-Agent": f"inspire-skill/{__version__}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        click.secho(f"✗ tarball fetch failed: {e}", fg="red", err=True)
        return None


def _extract_assets(tarball: bytes, dest: Path) -> Path | None:
    """Extract the tarball into `dest` and return the top-level extracted dir."""
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tf:
            # GitHub codeload tarballs use a single top-level dir like InspireSkill-main/
            members = tf.getmembers()
            if not members:
                return None
            top = members[0].name.split("/", 1)[0]
            tf.extractall(dest)
            extracted = dest / top
            return extracted if extracted.is_dir() else None
    except (tarfile.TarError, OSError) as e:
        click.secho(f"✗ tarball extract failed: {e}", fg="red", err=True)
        return None


def _refresh_skill_files(silent: bool) -> bool:
    harnesses = _detect_harnesses()
    if not harnesses:
        if not silent:
            click.secho(
                "! No agent harness detected "
                "(checked ~/.claude, ~/.codex, ~/.gemini, ~/.openclaw, "
                "$OPENCODE_CONFIG_DIR or ~/.config/opencode); "
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
            # Wipe any previous install (symlinks from dev mode, or stale files).
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
    else:
        click.secho(f"✓ InspireSkill is up to date (v{current}).", fg="green")


def _is_dev_install() -> bool:
    """True when this process is running out of an editable / dev checkout.

    We refuse destructive refreshes (CLI upgrade via `uv tool`, SKILL dir wipe)
    in that case to avoid clobbering the maintainer's local edits.
    """
    if _detect_installer() is not None:
        return False
    # Any harness skill dir currently living as a symlink is a strong signal
    # that scripts/install-dev.sh wired this machine up.
    for target in HARNESS_SKILL_DIRS.values():
        skill = target / "SKILL.md"
        if skill.is_symlink():
            return True
    return False


@click.command("update")
@click.option("--check", "check_only", is_flag=True, help="Only check upstream; don't upgrade.")
@click.option("--silent", is_flag=True, help="Suppress output (used by background checks).")
@click.option("--cli-only", is_flag=True, help="Upgrade the Python package only.")
@click.option("--skill-only", is_flag=True, help="Refresh SKILL.md + references/ only.")
@click.option(
    "--force",
    is_flag=True,
    help="Override the dev-install safety check (will clobber local symlinks).",
)
def update(check_only: bool, silent: bool, cli_only: bool, skill_only: bool, force: bool) -> None:
    """Check for and install newer InspireSkill versions."""
    if cli_only and skill_only:
        raise click.UsageError("--cli-only and --skill-only are mutually exclusive.")

    # --- check path -------------------------------------------------------
    if check_only:
        result = run_check(write=True)
        _print_status(result, silent)
        if not result.get("latest"):
            sys.exit(1)
        return

    # --- dev-install guard -----------------------------------------------
    if _is_dev_install() and not force:
        click.secho(
            "✗ This looks like a dev install (editable CLI or symlinked SKILL dirs).\n"
            "  Running `inspire update` would clobber your local edits.\n"
            "  Pull and re-sync instead:\n"
            "      git -C <repo> pull --ff-only && <repo>/scripts/install-dev.sh\n"
            "  Pass --force to override.",
            fg="red",
            err=True,
        )
        sys.exit(2)

    # --- upgrade path -----------------------------------------------------
    # Always refresh the version cache first so subsequent invocations show
    # the correct state and the notice goes away if we successfully upgrade.
    pre = run_check(write=True)
    if not silent:
        _print_status(pre, silent=False)

    ok = True
    if not skill_only:
        ok = _upgrade_cli(silent) and ok
    if not cli_only:
        ok = _refresh_skill_files(silent) and ok

    # Re-check after upgrade so the cache reflects the new local version.
    run_check(write=True)

    if not ok:
        sys.exit(1)
    if not silent:
        click.secho("✓ InspireSkill updated.", fg="green", bold=True)
