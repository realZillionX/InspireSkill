"""Tests for detecting supported global installer layouts.

The detector must inspect the virtual-environment root directly so that
``uv tool`` and ``pipx`` installations remain distinguishable from local
virtual environments and system Python.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from inspire.cli.main import main as cli_main

from inspire.cli.commands.update import (
    _detect_installer,
    _ensure_global_playwright_runtime,
    _ensure_playwright_runtime,
    _kimi_code_home,
    _kimi_desktop_root,
    _release_entries_between,
    _is_local_requirement,
    _parse_uv_tool_list,
    ReleaseEntry,
    _upgrade_cli,
)

update_module = importlib.import_module("inspire.cli.commands.update")


@pytest.mark.parametrize(
    "prefix, expected",
    [
        # uv tool install — the layout that triggered the bug report.
        ("/Users/vagrant/.local/share/uv/tools/inspire-skill", "uv"),
        # uv tool install on Linux user dir.
        ("/home/alice/.local/share/uv/tools/inspire-skill", "uv"),
        # pipx — symmetric layout.
        ("/Users/vagrant/.local/share/pipx/venvs/inspire-skill", "pipx"),
        ("/home/alice/.local/share/pipx/venvs/inspire-skill", "pipx"),
        # Unmanaged local venv. Must return None so update.py reports the
        # official installer as the recovery path, not the `uv tool` branch.
        ("/Users/zillionx/InspireSkill/cli/.venv", None),
        # System Python — also None.
        ("/usr/local", None),
        ("/opt/homebrew", None),
        # Edge: a path that contains "uv" or "tools" alone is NOT enough
        # — both segments must be present for "uv" to match. Same for
        # pipx (needs both "pipx" and "venvs").
        ("/Users/x/uv/random/dir", None),
        ("/Users/x/tools/something", None),
        ("/Users/x/pipx/random/dir", None),
        ("/Users/x/venvs/something", None),
    ],
)
def test_detect_installer_from_prefix(
    prefix: str,
    expected: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "prefix", prefix)
    assert _detect_installer() == expected


def test_detect_harnesses_includes_all_supported_desktop_and_cli_harnesses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = {
        "claude": tmp_path / ".claude",
        "antigravity": tmp_path / ".gemini",
        "cursor": tmp_path / ".cursor",
        "qoder": tmp_path / ".qoder",
        "qoder-work": tmp_path / ".qoderwork",
        "kimi-code": tmp_path / ".kimi-code",
        "kimi-desktop": tmp_path / "Library" / "Application Support" / "kimi-desktop",
        "opencode": tmp_path / ".config" / "opencode",
    }
    roots["claude"].mkdir()
    roots["antigravity"].mkdir()
    roots["cursor"].mkdir()
    roots["qoder"].mkdir()
    roots["qoder-work"].mkdir()
    roots["kimi-code"].mkdir()
    roots["kimi-desktop"].mkdir(parents=True)
    monkeypatch.setattr(update_module, "HARNESS_ROOTS", roots)

    assert update_module._detect_harnesses() == [
        "claude",
        "antigravity",
        "cursor",
        "qoder",
        "qoder-work",
        "kimi-code",
        "kimi-desktop",
    ]


def test_antigravity_skill_dir_uses_google_global_config_path() -> None:
    assert update_module.HARNESS_SKILL_DIRS["antigravity"] == (
        Path.home() / ".gemini" / "config" / "skills" / "inspire"
    )
    assert "gemini" not in update_module.HARNESS_SKILL_DIRS
    assert "gemini" not in update_module.HARNESS_ROOTS


def test_cursor_skill_dir_uses_cursor_global_skills_path() -> None:
    assert update_module.HARNESS_SKILL_DIRS["cursor"] == (
        Path.home() / ".cursor" / "skills" / "inspire"
    )


def test_kimi_code_skill_dir_uses_kimi_code_global_skills_path() -> None:
    assert update_module.HARNESS_SKILL_DIRS["kimi-code"] == (
        Path.home() / ".kimi-code" / "skills" / "inspire"
    )


def test_qoder_work_skill_dir_uses_qoder_work_global_skills_path() -> None:
    assert update_module.HARNESS_SKILL_DIRS["qoder-work"] == (
        Path.home() / ".qoderwork" / "skills" / "inspire"
    )


def test_kimi_desktop_skill_dir_uses_daemon_shared_skills_path() -> None:
    expected_root = (
        Path.home()
        / "Library"
        / "Application Support"
        / "kimi-desktop"
        / "daimon-share"
        / "daimon"
    )
    assert _kimi_desktop_root() == expected_root
    assert update_module.HARNESS_ROOTS["kimi-desktop"] == expected_root
    assert update_module.HARNESS_SKILL_DIRS["kimi-desktop"] == (
        expected_root / "skills" / "inspire"
    )


def test_kimi_code_home_respects_environment_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_home = tmp_path / "custom-kimi-code"
    monkeypatch.setenv("KIMI_CODE_HOME", str(custom_home))

    assert _kimi_code_home() == custom_home


def test_kimi_code_harness_paths_respect_environment_at_module_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_home = tmp_path / "custom-kimi-code"
    monkeypatch.setenv("KIMI_CODE_HOME", str(custom_home))
    reloaded = importlib.reload(update_module)

    try:
        assert reloaded.HARNESS_ROOTS["kimi-code"] == custom_home
        assert reloaded.HARNESS_SKILL_DIRS["kimi-code"] == (
            custom_home / "skills" / "inspire"
        )
    finally:
        monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
        importlib.reload(update_module)


def test_upgrade_cli_retries_pypi_network_errors_with_mirrors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], str | None]] = []

    def fake_run(cmd, check, env, text, stdout, stderr, encoding=None, errors=None):
        calls.append((cmd, None if env is None else env.get("UV_DEFAULT_INDEX")))
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                cmd,
                1,
                stdout="Resolving dependencies...\n",
                stderr=(
                    "error: Failed to fetch: `https://pypi.org/simple/inspire-skill/`\n"
                    "  Caused by: operation timed out\n"
                ),
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="upgraded\n", stderr="")

    monkeypatch.setattr(sys, "prefix", "/Users/vagrant/.local/share/uv/tools/inspire-skill")
    monkeypatch.setattr(update_module, "_uv_tool_info", lambda: None)
    monkeypatch.setattr(update_module.subprocess, "run", fake_run)

    assert _upgrade_cli(silent=True) is True
    assert calls == [
        (["uv", "tool", "install", "--force", "--refresh", "inspire-skill"], None),
        (
            ["uv", "tool", "install", "--force", "--refresh", "inspire-skill"],
            "https://pypi.tuna.tsinghua.edu.cn/simple",
        ),
    ]


def test_upgrade_cli_default_output_does_not_forward_installer_details(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = 0

    def fake_run(cmd, check, env, text, stdout, stderr, encoding=None, errors=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(
                cmd,
                1,
                stdout="Using Python /private/tmp/tool/bin/python\n",
                stderr=(
                    "Failed to fetch https://pypi.org/simple/inspire-skill/?token=secret\n"
                    "Run uv tool install --force inspire-skill\n"
                ),
            )
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Installed from https://pypi.tuna.tsinghua.edu.cn/simple\n",
            stderr="",
        )

    monkeypatch.setattr(sys, "prefix", "/Users/vagrant/.local/share/uv/tools/inspire-skill")
    monkeypatch.setattr(update_module, "_uv_tool_info", lambda: None)
    monkeypatch.setattr(update_module.subprocess, "run", fake_run)

    assert _upgrade_cli(silent=False) is True
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_completed_process_debug_log_scrubs_secrets_urls_and_paths(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cmd = [
        "/Users/alice/.local/bin/uv",
        "tool",
        "install",
        "https://user:basic-secret@example.test/pkg?token=query-secret&channel=stable",
        "--api-key",
        "command-secret",
    ]
    proc = subprocess.CompletedProcess(
        cmd,
        1,
        stdout=(
            "download https://download-user:download-pass@example.test/archive"
            "?access_token=stdout-query\n"
            "password=hunter2 api_key=stdout-key path=/Users/alice/private/cache\n"
            "normal diagnostic remains\n"
        ),
        stderr='{"token": "stderr-token"} --password stderr-password\n',
    )

    with caplog.at_level("DEBUG", logger=update_module.__name__):
        update_module._log_completed_process("package upgrade", proc, cmd=cmd)

    rendered = caplog.text
    for secret in (
        "basic-secret",
        "query-secret",
        "command-secret",
        "download-user",
        "download-pass",
        "stdout-query",
        "hunter2",
        "stdout-key",
        "stderr-token",
        "stderr-password",
        "/Users/alice",
    ):
        assert secret not in rendered
    assert "example.test/pkg?<redacted>" in rendered
    assert "example.test/archive?<redacted>" in rendered
    assert "<redacted>" in rendered
    assert "<path>" in rendered
    assert "normal diagnostic remains" in rendered


def test_suppressed_child_debug_log_is_scrubbed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("DEBUG", logger=update_module.__name__):
        with update_module._suppress_subprocess_output():
            print("token=python-secret /Users/alice/private")
            os.write(
                2,
                b"https://user:fd-secret@example.test/runtime?password=query-secret\n",
            )

    rendered = caplog.text
    assert "python-secret" not in rendered
    assert "fd-secret" not in rendered
    assert "query-secret" not in rendered
    assert "/Users/alice" not in rendered
    assert "<redacted>" in rendered
    assert "<path>" in rendered


def test_upgrade_cli_pins_known_target_version_for_uv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, check, env, text, stdout, stderr, encoding=None, errors=None):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="installed\n", stderr="")

    monkeypatch.setattr(sys, "prefix", "/Users/vagrant/.local/share/uv/tools/inspire-skill")
    monkeypatch.setattr(update_module.subprocess, "run", fake_run)

    assert _upgrade_cli(silent=True, target_version="5.1.21") is True
    assert calls == [
        ["uv", "tool", "install", "--force", "--refresh", "inspire-skill==5.1.21"]
    ]


def test_upgrade_cli_does_not_retry_non_network_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, check, env, text, stdout, stderr, encoding=None, errors=None):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            2,
            stdout="",
            stderr="error: unrecognized option '--bad-flag'\n",
        )

    monkeypatch.setattr(sys, "prefix", "/Users/vagrant/.local/share/pipx/venvs/inspire-skill")
    monkeypatch.setattr(update_module.subprocess, "run", fake_run)

    assert _upgrade_cli(silent=True) is False
    assert calls == [["pipx", "upgrade", "inspire-skill"]]


def test_upgrade_cli_from_repo_venv_updates_global_uv_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, check, env, text, stdout, stderr, encoding=None, errors=None):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="installed\n", stderr="")

    monkeypatch.setattr(sys, "prefix", "/Users/zillionx/InspireSkill/cli/.venv")
    monkeypatch.setattr(
        update_module,
        "_uv_tool_info",
        lambda: update_module.UvToolInfo(
            version="4.1.0",
            required="file:///Users/zillionx/InspireSkill/cli",
            env_path="/Users/zillionx/.local/share/uv/tools/inspire-skill",
            executable_path="/Users/zillionx/.local/bin/inspire",
        ),
    )
    monkeypatch.setattr(update_module.subprocess, "run", fake_run)

    assert _upgrade_cli(silent=True) is True
    assert calls == [["uv", "tool", "install", "--force", "--refresh", "inspire-skill"]]


def test_update_runtime_check_installs_missing_playwright_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness = iter([False, True])
    install_kwargs: list[dict[str, object]] = []
    monkeypatch.setattr(update_module, "_playwright_chromium_available", lambda: next(readiness))
    monkeypatch.setattr(
        update_module,
        "_install_playwright_chromium",
        lambda **kwargs: install_kwargs.append(kwargs) or True,
    )

    assert _ensure_playwright_runtime(silent=True) is True
    assert install_kwargs == [{"include_system_deps": None}]


def test_update_runtime_check_fails_if_playwright_still_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(update_module, "_playwright_chromium_available", lambda: False)
    monkeypatch.setattr(update_module, "_install_playwright_chromium", lambda **_kwargs: True)

    assert _ensure_playwright_runtime(silent=True) is False


def test_update_runtime_check_suppresses_playwright_installer_output(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    readiness = iter([False, True])

    def noisy_install(**_kwargs) -> bool:
        print("playwright stdout /private/tmp/browser")
        print("playwright stderr https://browser.example/download", file=sys.stderr)
        os.write(1, b"child stdout\n")
        os.write(2, b"child stderr\n")
        return True

    monkeypatch.setattr(update_module, "_playwright_chromium_available", lambda: next(readiness))
    monkeypatch.setattr(update_module, "_install_playwright_chromium", noisy_install)

    assert _ensure_playwright_runtime(silent=False) is True
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_global_runtime_setup_uses_global_inspire_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], str | None]] = []
    monkeypatch.setattr(
        update_module,
        "_uv_tool_info",
        lambda: update_module.UvToolInfo(
            executable_path="/Users/zillionx/.local/bin/inspire",
        ),
    )

    def fake_run(cmd, check, env, text, stdout, stderr, encoding=None, errors=None):
        calls.append((cmd, env.get("INSPIRE_SKIP_UPDATE_CHECK") if env else None))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(update_module.subprocess, "run", fake_run)

    assert _ensure_global_playwright_runtime(silent=True) is True
    assert calls == [
        (["/Users/zillionx/.local/bin/inspire", "_ensure-playwright-runtime", "--silent"], "1")
    ]


def test_global_runtime_setup_does_not_forward_child_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        update_module,
        "_uv_tool_info",
        lambda: update_module.UvToolInfo(
            executable_path="/Users/zillionx/.local/bin/inspire",
        ),
    )
    monkeypatch.setattr(
        update_module.subprocess,
        "run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Python: /private/tmp/tool/bin/python\n",
            stderr="playwright install chromium\n",
        ),
    )

    assert _ensure_global_playwright_runtime(silent=False) is True
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_update_runs_global_runtime_setup_after_cli_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(update_module, "run_check", lambda **_kwargs: {"latest": "4.1.1"})
    monkeypatch.setattr(update_module, "_print_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        update_module,
        "_upgrade_cli",
        lambda silent, target_version=None: calls.append(f"cli:{target_version}") or True,
    )
    monkeypatch.setattr(
        update_module,
        "_refresh_skill_files",
        lambda silent: calls.append("skills") or True,
    )
    monkeypatch.setattr(
        update_module,
        "_audit_update_state",
        lambda **_kwargs: (calls.append("audit") or True, "4.1.1"),
    )
    monkeypatch.setattr(
        update_module,
        "_ensure_global_playwright_runtime",
        lambda silent: calls.append("runtime") or True,
    )
    update_module.update.callback(
        check_only=False,
        silent=True,
        cli_only=False,
        skill_only=False,
        assume_yes=False,
    )

    assert calls == ["cli:4.1.1", "skills", "audit", "runtime"]


def test_update_delegates_post_upgrade_work_to_new_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(update_module, "__version__", "5.2.2")
    monkeypatch.setattr(
        update_module,
        "run_check",
        lambda **_kwargs: {"current": "5.2.2", "latest": "5.2.3"},
    )
    monkeypatch.setattr(update_module, "_print_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        update_module,
        "_upgrade_cli",
        lambda silent, target_version=None: calls.append(f"cli:{target_version}") or True,
    )
    monkeypatch.setattr(
        update_module,
        "_run_post_update_command",
        lambda **kwargs: calls.append(f"post:{kwargs['expected_version']}") or True,
    )
    monkeypatch.setattr(
        update_module,
        "_refresh_skill_files",
        lambda *_args, **_kwargs: calls.append("old-skill-refresh") or True,
    )

    update_module.update.callback(
        check_only=False,
        silent=True,
        cli_only=False,
        skill_only=False,
        assume_yes=False,
    )

    assert calls == ["cli:5.2.3", "post:5.2.3"]


def test_download_tarball_uses_only_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Response:
        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"tarball"

    calls: list[tuple[object, int]] = []
    monkeypatch.setattr(
        update_module.urllib.request,
        "urlopen",
        lambda request, *, timeout: calls.append((request, timeout)) or _Response(),
    )

    assert update_module._download_tarball(timeout=7) == b"tarball"
    assert len(calls) == 1
    assert calls[0][1] == 7


def _skill_tarball(entries: dict[str, bytes]) -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        for name, content in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return payload.getvalue()


def test_extract_assets_copies_only_the_single_wrapped_tree(tmp_path: Path) -> None:
    destination = tmp_path / "extract"
    extracted = update_module._extract_assets(
        _skill_tarball(
            {
                "InspireSkill-main/SKILL.md": b"skill\n",
                "InspireSkill-main/references/setup.md": b"reference\n",
            }
        ),
        destination,
    )

    assert extracted == destination / "InspireSkill-main"
    assert (extracted / "SKILL.md").read_bytes() == b"skill\n"
    assert (extracted / "references" / "setup.md").read_bytes() == b"reference\n"


@pytest.mark.parametrize(
    "malicious_name",
    [
        "InspireSkill-main/../../escaped.txt",
        "InspireSkill-main\\..\\escaped.txt",
        "InspireSkill-main/SKILL.md:alternate-stream",
        "/InspireSkill-main/escaped.txt",
    ],
)
def test_extract_assets_rejects_unsafe_paths_before_writing(
    tmp_path: Path,
    malicious_name: str,
) -> None:
    destination = tmp_path / "extract"
    extracted = update_module._extract_assets(
        _skill_tarball(
            {
                "InspireSkill-main/SKILL.md": b"must not be partially written\n",
                malicious_name: b"escaped\n",
            }
        ),
        destination,
    )

    assert extracted is None
    assert not (destination / "InspireSkill-main" / "SKILL.md").exists()
    assert not (tmp_path / "escaped.txt").exists()


def test_extract_assets_rejects_links_before_writing(tmp_path: Path) -> None:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        skill = tarfile.TarInfo("InspireSkill-main/SKILL.md")
        skill.size = 6
        archive.addfile(skill, io.BytesIO(b"skill\n"))
        link = tarfile.TarInfo("InspireSkill-main/references")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        archive.addfile(link)

    destination = tmp_path / "extract"
    assert update_module._extract_assets(payload.getvalue(), destination) is None
    assert not (destination / "InspireSkill-main" / "SKILL.md").exists()


def _stub_successful_update(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive `update()` through the happy path without touching the network."""

    def fake_run_check(**kwargs):
        if kwargs.get("current_version"):
            return {"current": kwargs.get("current_version"), "latest": "5.2.3"}
        return {"current": "5.2.1", "latest": "5.2.3"}

    monkeypatch.setattr(update_module, "run_check", fake_run_check)
    monkeypatch.setattr(update_module, "_upgrade_cli", lambda *args, **_kwargs: True)
    monkeypatch.setattr(update_module, "_refresh_skill_files", lambda *args, **_kwargs: True)
    monkeypatch.setattr(update_module, "_audit_update_state", lambda **_kwargs: (True, "5.2.3"))
    monkeypatch.setattr(update_module, "_ensure_global_playwright_runtime", lambda silent: True)
    monkeypatch.setattr(
        update_module,
        "_installed_skill_harnesses",
        lambda: ["claude", "codex"],
    )
    monkeypatch.setattr(
        update_module,
        "_fetch_release_entries",
        lambda: [
            ReleaseEntry(tag="v5.2.3", body="## 更新内容\n\n### 新增\n\n- 新增 Cursor Harness 支持。"),
            ReleaseEntry(tag="v5.2.2", body="## 更新内容\n\n### 修复\n\n- 修复 Antigravity 安装目录。"),
            ReleaseEntry(tag="v5.2.1", body="## 更新内容\n\n### 新增\n\n- Qoder。"),
        ],
    )


def test_update_reports_progress_refreshed_harnesses_and_release_notes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_successful_update(monkeypatch)

    update_module.update.callback(
        check_only=False,
        silent=False,
        cli_only=False,
        skill_only=False,
        assume_yes=False,
    )

    assert capsys.readouterr().out.splitlines() == [
        "› Checking for updates...",
        "› Updating CLI...",
        "› Refreshing agent skills...",
        "› Verifying installation...",
        "› Preparing browser runtime...",
        "InspireSkill updated to v5.2.3.",
        "Skills refreshed: claude, codex.",
        "What's new (v5.2.1 → v5.2.3):",
        "- v5.2.3: 新增 Cursor Harness 支持。",
        "- v5.2.2: 修复 Antigravity 安装目录。",
    ]


def test_update_cli_only_does_not_claim_a_skill_refresh(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_successful_update(monkeypatch)

    update_module.update.callback(
        check_only=False,
        silent=False,
        cli_only=True,
        skill_only=False,
        assume_yes=False,
    )

    output = capsys.readouterr().out
    assert "Skills refreshed" not in output
    assert "› Refreshing agent skills..." not in output
    assert "What's new (v5.2.1 → v5.2.3):" in output


def test_update_skill_only_reports_harnesses_without_release_notes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_successful_update(monkeypatch)

    update_module.update.callback(
        check_only=False,
        silent=False,
        cli_only=False,
        skill_only=True,
        assume_yes=False,
    )

    output = capsys.readouterr().out
    assert "Skills refreshed: claude, codex." in output
    # --skill-only leaves the package version alone, so there is no upgrade to
    # summarize.
    assert "What's new" not in output


def test_update_reports_release_notes_after_the_self_upgrade_handoff(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The handoff path returns early — its summary must not be skipped.

    The skill refresh happens inside the newly installed CLI, whose output is
    captured for debug logs, so the harness list has to be read back from disk
    by the process that prints the summary.
    """
    _stub_successful_update(monkeypatch)
    monkeypatch.setattr(update_module, "__version__", "5.2.1")
    monkeypatch.setattr(update_module, "_run_post_update_command", lambda **_kwargs: True)

    update_module.update.callback(
        check_only=False,
        silent=False,
        cli_only=False,
        skill_only=False,
        assume_yes=False,
    )

    assert capsys.readouterr().out.splitlines() == [
        "› Checking for updates...",
        "› Updating CLI...",
        "› Completing setup...",
        "InspireSkill updated to v5.2.3.",
        "Skills refreshed: claude, codex.",
        "What's new (v5.2.1 → v5.2.3):",
        "- v5.2.3: 新增 Cursor Harness 支持。",
        "- v5.2.2: 修复 Antigravity 安装目录。",
    ]


def test_update_json_carries_skills_and_release_notes_without_progress_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_successful_update(monkeypatch)

    result = CliRunner().invoke(cli_main, ["--json", "update"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "success": True,
        "data": {
            "version": "5.2.3",
            "updated": True,
            "skills": ["claude", "codex"],
            "release_notes": [
                {"version": "5.2.3", "summary": "新增 Cursor Harness 支持。"},
                {"version": "5.2.2", "summary": "修复 Antigravity 安装目录。"},
            ],
        },
    }


def test_installed_skill_harnesses_lists_only_harnesses_holding_a_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = tmp_path / "claude" / "skills" / "inspire"
    installed.mkdir(parents=True)
    (installed / "SKILL.md").write_text("# Inspire\n", encoding="utf-8")
    empty = tmp_path / "codex" / "skills" / "inspire"
    empty.mkdir(parents=True)

    monkeypatch.setattr(update_module, "_detect_harnesses", lambda: ["claude", "codex"])
    monkeypatch.setattr(
        update_module,
        "HARNESS_SKILL_DIRS",
        {"claude": installed, "codex": empty},
    )

    assert update_module._installed_skill_harnesses() == ["claude"]


def test_release_entries_between_includes_versions_between_old_and_new() -> None:
    entries = _release_entries_between(
        [
            ReleaseEntry(tag="v5.2.3", body="## 更新内容\n\n### 新增\n\n- C"),
            ReleaseEntry(tag="v5.2.2", body="## 更新内容\n\n### 新增\n\n- B"),
            ReleaseEntry(tag="v5.2.1", body="## 更新内容\n\n### 修复\n\n- A"),
        ],
        previous_version="5.2.1",
        new_version="5.2.3",
    )

    assert [(entry.tag, entry.body.strip()) for entry in entries] == [
        ("v5.2.3", "## 更新内容\n\n### 新增\n\n- C"),
        ("v5.2.2", "## 更新内容\n\n### 新增\n\n- B"),
    ]


def test_release_entries_from_changelog_text_parses_release_sections() -> None:
    entries = update_module._release_entries_from_changelog_text(
        "# Changelog\n\n"
        "## Unreleased\n\n"
        "- 未发布内容。\n\n"
        "## v6.3.0\n\n"
        "### 修复\n\n"
        "- 修复摘要兜底。\n\n"
        "## v6.2.0\n\n"
        "### 新增\n\n"
        "- 新增 Cursor。\n"
    )

    assert [(entry.tag, entry.body.strip()) for entry in entries] == [
        ("v6.3.0", "### 修复\n\n- 修复摘要兜底。"),
        ("v6.2.0", "### 新增\n\n- 新增 Cursor。"),
    ]


def test_release_items_join_hard_wrapped_continuation_lines() -> None:
    items = update_module._release_items_for_display(
        "### 破坏性变更\n\n"
        "- 移除 `inspire job id`、`inspire hpc id`、`inspire notebook id`。CLI 不再有任何\n"
        "  Handle 输出入口，用 `list` 拿 Name 即可。\n"
        "- `serving create --shm-gib` 改名为 `--shm-size`。\n"
    )

    assert items == [
        "移除 `inspire job id`、`inspire hpc id`、`inspire notebook id`。"
        "CLI 不再有任何 Handle 输出入口，用 `list` 拿 Name 即可。",
        "`serving create --shm-gib` 改名为 `--shm-size`。",
    ]


def test_fetch_release_entries_falls_back_to_changelog_when_github_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback_entries = [ReleaseEntry(tag="v5.2.3", body="## 更新内容\n\n- 兜底摘要。")]

    monkeypatch.setattr(update_module, "_fetch_release_entries_from_github", lambda timeout=10: [])
    monkeypatch.setattr(
        update_module,
        "_fetch_release_entries_from_changelog",
        lambda timeout=10: fallback_entries,
    )

    assert update_module._fetch_release_entries() == fallback_entries


def test_release_summary_is_bounded_and_removes_engineering_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_item = "A" * 300
    monkeypatch.setattr(
        update_module,
        "_fetch_release_entries",
        lambda: [
            ReleaseEntry(
                tag="v5.2.4",
                body=(
                    "## 更新内容\n\n"
                    "- 用户可见改进 https://example.com/details?token=secret\n"
                    "- 修复 /Users/alice/private/config.toml 的兼容性\n"
                    "- uv tool install --force --refresh inspire-skill\n"
                    f"- {long_item}\n"
                    "- 第五项\n"
                    "- 第六项\n"
                    "- 第七项\n"
                ),
            ),
            ReleaseEntry(tag="v5.2.3", body="- 不应超过总上限\n"),
            ReleaseEntry(tag="v5.2.2", body="- 旧版本摘要\n"),
            ReleaseEntry(tag="v5.2.1", body="- 不应读取第四个 release\n"),
        ],
    )

    items = update_module._release_summary_items("5.2.0", "5.2.4")

    rendered = "\n".join(f"- v{version}: {item}" for version, item in items)
    assert len(items) <= update_module._RELEASE_SUMMARY_MAX_ITEMS
    assert "https://" not in rendered
    assert "/Users/alice" not in rendered
    assert "uv tool install" not in rendered
    assert "A" * update_module._RELEASE_SUMMARY_ITEM_MAX_CHARS not in rendered
    assert "…" in rendered
    assert "不应读取第四个 release" not in rendered


def test_update_failure_output_is_one_compact_actionable_hint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        update_module,
        "run_check",
        lambda **_kwargs: {"current": "5.2.1", "latest": "5.2.2"},
    )
    monkeypatch.setattr(update_module, "_upgrade_cli", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(update_module, "_refresh_skill_files", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(update_module, "_audit_update_state", lambda **_kwargs: (False, None))

    with pytest.raises(SystemExit):
        update_module.update.callback(
            check_only=False,
            silent=False,
            cli_only=False,
            skill_only=False,
            assume_yes=False,
        )

    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        "✗ InspireSkill update failed. Retry with `inspire --debug update` for diagnostics."
    ]
    combined = captured.out + captured.err
    assert "/Users/" not in combined
    assert "uv tool install" not in combined
    assert "https://" not in combined


def test_update_silent_mode_is_fully_quiet(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        update_module,
        "run_check",
        lambda **_kwargs: {"current": "5.2.1", "latest": "5.2.1"},
    )
    monkeypatch.setattr(update_module, "_upgrade_cli", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(update_module, "_refresh_skill_files", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(update_module, "_audit_update_state", lambda **_kwargs: (True, "5.2.1"))
    monkeypatch.setattr(update_module, "_ensure_global_playwright_runtime", lambda _silent: True)
    update_module.update.callback(
        check_only=False,
        silent=True,
        cli_only=False,
        skill_only=False,
        assume_yes=False,
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_parse_uv_tool_list_captures_local_source_and_executable() -> None:
    info = _parse_uv_tool_list(
        "inspire-skill v4.1.1 [required: file:///Users/zillionx/InspireSkill/cli] "
        "(/Users/zillionx/.local/share/uv/tools/inspire-skill)\n"
        "- inspire (/Users/zillionx/.local/bin/inspire)\n"
    )

    assert info is not None
    assert info.version == "4.1.1"
    assert info.required == "file:///Users/zillionx/InspireSkill/cli"
    assert info.env_path == "/Users/zillionx/.local/share/uv/tools/inspire-skill"
    assert info.executable_path == "/Users/zillionx/.local/bin/inspire"
    assert _is_local_requirement(info.required)


def test_global_audit_prefers_uv_tool_executable_over_repo_venv_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(
        update_module,
        "_uv_tool_info",
        lambda: update_module.UvToolInfo(
            version="4.1.1",
            required=None,
            env_path="/Users/zillionx/.local/share/uv/tools/inspire-skill",
            executable_path="/Users/zillionx/.local/bin/inspire",
        ),
    )
    monkeypatch.setattr(update_module.shutil, "which", lambda _name: "/repo/.venv/bin/inspire")

    def fake_run(cmd, check, env, text, stdout, stderr, encoding=None, errors=None):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="inspire, version 4.1.1\n", stderr="")

    monkeypatch.setattr(update_module.subprocess, "run", fake_run)

    ok, actual = update_module._audit_global_cli(expected_version="4.1.1", silent=True)

    assert ok is True
    assert actual == "4.1.1"
    assert calls == [["/Users/zillionx/.local/bin/inspire", "--version"]]
