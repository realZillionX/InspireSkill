"""Coverage for `inspire uninstall` and the `install.sh --uninstall` fallback.

Every test runs against a fake ``$HOME``. Nothing here may touch the real one:
the command under test deletes directories.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from inspire.cli.main import main as cli_main

# `inspire.cli.commands.<name>` resolves to the Click command once the package
# finishes importing, so reach the modules by import, not by attribute.
uninstall_module = importlib.import_module("inspire.cli.commands.uninstall")
update_module = importlib.import_module("inspire.cli.commands.update")

INSTALLER = Path(__file__).resolve().parents[1].parent / "scripts" / "install.sh"


@pytest.fixture
def fake_home(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    """A populated $HOME that looks like a finished install."""
    home = tmp_path / "home"
    skills = {
        "claude": home / ".claude" / "skills" / "inspire",
        "codex": home / ".codex" / "skills" / "inspire",
    }
    for target in skills.values():
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("# Inspire Skill\n", encoding="utf-8")

    plist = home / "Library" / "LaunchAgents" / "sh.inspire-skill.update-check.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>\n", encoding="utf-8")
    log = home / "Library" / "Logs" / "inspire-skill-update-check.log"
    log.parent.mkdir(parents=True)
    log.write_text("checked\n", encoding="utf-8")

    account = home / ".inspire" / "accounts" / "alice"
    account.mkdir(parents=True)
    (account / "config.toml").write_text('user = "alice"\n', encoding="utf-8")
    (home / ".inspire" / "update-status.json").write_text("{}", encoding="utf-8")

    browsers = home / "Library" / "Caches" / "ms-playwright"
    browsers.mkdir(parents=True)
    (browsers / "chromium-1234").mkdir()

    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setattr(uninstall_module, "HARNESS_SKILL_DIRS", skills)
    monkeypatch.setattr(uninstall_module.sys, "platform", "darwin")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    # No package removal unless a test asks for one; every other test would
    # otherwise fall into the hard-exit path.
    monkeypatch.setattr(uninstall_module, "_package_uninstall_command", lambda: None)
    monkeypatch.setattr(uninstall_module, "_unload_launch_agent", lambda: None)
    return home


def _invoke(*args: str):  # noqa: ANN202
    return CliRunner().invoke(cli_main, ["uninstall", *args])


def test_default_tier_removes_install_output_and_keeps_credentials(fake_home) -> None:  # noqa: ANN001
    result = _invoke("--yes")

    assert result.exit_code == 0, result.output
    assert not (fake_home / ".claude" / "skills" / "inspire").exists()
    assert not (fake_home / ".codex" / "skills" / "inspire").exists()
    assert not (
        fake_home / "Library" / "LaunchAgents" / "sh.inspire-skill.update-check.plist"
    ).exists()
    assert not (fake_home / "Library" / "Logs" / "inspire-skill-update-check.log").exists()
    assert not (fake_home / ".inspire" / "update-status.json").exists()
    # Kept without --purge.
    assert (fake_home / ".inspire" / "accounts" / "alice" / "config.toml").is_file()
    # Kept without --purge-runtime.
    assert (fake_home / "Library" / "Caches" / "ms-playwright").is_dir()


def test_purge_removes_the_inspire_home(fake_home) -> None:  # noqa: ANN001
    result = _invoke("--yes", "--purge")

    assert result.exit_code == 0, result.output
    assert not (fake_home / ".inspire").exists()
    assert (fake_home / "Library" / "Caches" / "ms-playwright").is_dir()


def test_purge_runtime_removes_the_shared_browser_cache(fake_home) -> None:  # noqa: ANN001
    result = _invoke("--yes", "--purge-runtime")

    assert result.exit_code == 0, result.output
    assert not (fake_home / "Library" / "Caches" / "ms-playwright").exists()
    assert (fake_home / ".inspire" / "accounts" / "alice" / "config.toml").is_file()


def test_plan_names_what_is_kept_and_the_flag_that_would_remove_it(fake_home) -> None:  # noqa: ANN001
    result = _invoke("--yes")

    assert "About to remove:" in result.output
    assert "~/.claude/skills/inspire" in result.output
    assert "Keeping:" in result.output
    assert "--purge" in result.output
    assert "--purge-runtime" in result.output
    # Home-relative only: the plan must not leak the local username.
    assert str(fake_home) not in result.output


def test_declining_the_prompt_removes_nothing(fake_home) -> None:  # noqa: ANN001
    result = CliRunner().invoke(cli_main, ["uninstall"], input="n\n")

    assert result.exit_code != 0
    assert (fake_home / ".claude" / "skills" / "inspire" / "SKILL.md").is_file()
    assert (fake_home / ".inspire" / "update-status.json").is_file()


def test_json_output_requires_explicit_confirmation(fake_home) -> None:  # noqa: ANN001
    result = CliRunner().invoke(cli_main, ["--json", "uninstall"])

    assert result.exit_code != 0
    assert "ConfirmationRequired" in result.output
    assert (fake_home / ".claude" / "skills" / "inspire" / "SKILL.md").is_file()


def test_json_output_reports_removed_and_kept(fake_home) -> None:  # noqa: ANN001
    result = CliRunner().invoke(cli_main, ["--json", "uninstall", "--yes"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert "claude skill" in payload["removed"]
    assert "account config" in payload["kept"]
    assert payload["package"] is None
    assert payload["uninstalled"] is False


def test_nothing_installed_is_not_an_error(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    empty = tmp_path / "empty-home"
    empty.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: empty))
    monkeypatch.setattr(uninstall_module, "HARNESS_SKILL_DIRS", {})
    monkeypatch.setattr(uninstall_module, "_package_uninstall_command", lambda: None)

    result = _invoke("--yes")

    assert result.exit_code == 0
    assert "Nothing to uninstall" in result.output


def test_package_is_removed_last_and_the_process_leaves_immediately(  # noqa: ANN001
    fake_home, monkeypatch
) -> None:
    calls: list[list[str]] = []
    exits: list[int] = []

    def _fake_run(cmd, **_kwargs):  # noqa: ANN001, ANN003
        calls.append(cmd)
        # The skill dirs must already be gone by the time the package goes.
        assert not (fake_home / ".claude" / "skills" / "inspire").exists()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(
        uninstall_module,
        "_package_uninstall_command",
        lambda: (["uv", "tool", "uninstall", "inspire-skill"], "uv"),
    )
    monkeypatch.setattr(uninstall_module.subprocess, "run", _fake_run)
    monkeypatch.setattr(uninstall_module, "_exit_now", exits.append)

    result = _invoke("--yes")

    assert calls == [["uv", "tool", "uninstall", "inspire-skill"]]
    assert exits == [0]
    assert "InspireSkill uninstalled." in result.output


def test_failed_package_removal_reports_and_exits_nonzero(fake_home, monkeypatch) -> None:  # noqa: ANN001
    exits: list[int] = []
    monkeypatch.setattr(
        uninstall_module,
        "_package_uninstall_command",
        lambda: (["uv", "tool", "uninstall", "inspire-skill"], "uv"),
    )
    monkeypatch.setattr(
        uninstall_module.subprocess,
        "run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 1, "", "boom"),
    )
    monkeypatch.setattr(uninstall_module, "_exit_now", exits.append)

    result = _invoke("--yes")

    assert exits == [1]
    assert "remove the package manually" in result.output


def test_a_blocked_removal_leaves_the_package_installed(fake_home, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        uninstall_module,
        "_package_uninstall_command",
        lambda: (["uv", "tool", "uninstall", "inspire-skill"], "uv"),
    )

    def _explode(cmd, **_kwargs):  # noqa: ANN001, ANN003
        raise AssertionError("package removal must not run after a failed deletion")

    monkeypatch.setattr(uninstall_module.subprocess, "run", _explode)
    monkeypatch.setattr(
        uninstall_module,
        "_remove_path",
        lambda path: "~/.claude/skills/inspire: Permission denied",
    )

    result = _invoke("--yes")

    assert result.exit_code != 0
    assert "left installed" in result.output


def test_launch_agent_is_unloaded_before_its_plist_is_deleted(fake_home, monkeypatch) -> None:  # noqa: ANN001
    plist = fake_home / "Library" / "LaunchAgents" / "sh.inspire-skill.update-check.plist"
    seen: list[bool] = []

    monkeypatch.setattr(
        uninstall_module,
        "_unload_launch_agent",
        lambda: seen.append(plist.exists()),
    )

    result = _invoke("--yes")

    assert result.exit_code == 0, result.output
    assert seen == [True]
    assert not plist.exists()


def test_project_assets_are_never_touched(fake_home, tmp_path) -> None:  # noqa: ANN001
    repo = tmp_path / "repo"
    (repo / ".inspire").mkdir(parents=True)
    (repo / ".inspire" / "config.toml").write_text("project = 'x'\n", encoding="utf-8")
    (repo / "INSPIRE.md").write_text("# assets\n", encoding="utf-8")

    assert _invoke("--yes", "--purge").exit_code == 0

    assert (repo / ".inspire" / "config.toml").is_file()
    assert (repo / "INSPIRE.md").is_file()


def test_playwright_browsers_path_zero_has_no_separate_cache(fake_home, monkeypatch) -> None:  # noqa: ANN001
    """`PLAYWRIGHT_BROWSERS_PATH=0` stores browsers inside the package."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")

    result = _invoke("--yes", "--purge-runtime")

    assert result.exit_code == 0, result.output
    assert "Playwright browsers" not in result.output


def test_cli_and_installer_agree_on_the_constants_they_both_encode() -> None:
    text = INSTALLER.read_text(encoding="utf-8")

    assert f'LAUNCH_LABEL="{uninstall_module.LAUNCH_AGENT_LABEL}"' in text
    assert uninstall_module.UPDATE_STATUS_FILENAME in text
    assert "ms-playwright" in text
    for harness in update_module.HARNESS_SKILL_DIRS:
        assert harness in text, harness


def test_installer_uninstall_clears_a_finished_install(tmp_path: Path) -> None:
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    home.mkdir()
    bin_dir.mkdir()

    for rel in (".claude/skills/inspire", ".codex/skills/inspire"):
        (home / rel).mkdir(parents=True)
        (home / rel / "SKILL.md").write_text("# Inspire Skill\n", encoding="utf-8")
    (home / ".inspire" / "accounts" / "alice").mkdir(parents=True)
    (home / ".inspire" / "update-status.json").write_text("{}", encoding="utf-8")
    # Pin the browser cache rather than letting the host platform pick it.
    browsers = home / "ms-playwright"
    browsers.mkdir()

    uninstalled = tmp_path / "uv-uninstalled"
    (bin_dir / "uv").write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1 $2" == "tool list" ]]; then echo "inspire-skill v7.0.0"; exit 0; fi\n'
        'if [[ "$1 $2" == "tool uninstall" ]]; then touch "$UV_UNINSTALL_MARKER"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    (bin_dir / "uv").chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "UV_UNINSTALL_MARKER": str(uninstalled),
        "PLAYWRIGHT_BROWSERS_PATH": str(browsers),
    }
    result = subprocess.run(
        ["bash", str(INSTALLER), "--uninstall", "--yes"],
        cwd=INSTALLER.parent.parent,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "unbound variable" not in result.stderr
    assert not (home / ".claude" / "skills" / "inspire").exists()
    assert not (home / ".codex" / "skills" / "inspire").exists()
    assert not (home / ".inspire" / "update-status.json").exists()
    assert (home / ".inspire" / "accounts" / "alice").is_dir()
    assert browsers.is_dir()
    assert uninstalled.exists()


def test_installer_uninstall_purge_tiers(tmp_path: Path) -> None:
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    home.mkdir()
    bin_dir.mkdir()
    (home / ".inspire" / "accounts" / "alice").mkdir(parents=True)
    browsers = home / "ms-playwright"
    browsers.mkdir()
    (bin_dir / "uv").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (bin_dir / "uv").chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PLAYWRIGHT_BROWSERS_PATH": str(browsers),
    }
    result = subprocess.run(
        ["bash", str(INSTALLER), "--uninstall", "--yes", "--purge", "--purge-runtime"],
        cwd=INSTALLER.parent.parent,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert not (home / ".inspire").exists()
    assert not browsers.exists()


def test_installer_uninstall_without_a_terminal_requires_yes(tmp_path: Path) -> None:
    """`curl | bash` leaves stdin pointing at the script, not at the user."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude" / "skills" / "inspire").mkdir(parents=True)

    result = subprocess.run(
        ["bash", str(INSTALLER), "--uninstall"],
        cwd=INSTALLER.parent.parent,
        env={**os.environ, "HOME": str(home)},
        text=True,
        capture_output=True,
        timeout=20,
        stdin=subprocess.DEVNULL,
        # A new session has no controlling terminal, so /dev/tty is unopenable —
        # the same position `curl | bash` puts the script in.
        start_new_session=True,
    )

    assert result.returncode != 0
    assert "--yes" in result.stderr
    assert (home / ".claude" / "skills" / "inspire").is_dir()
