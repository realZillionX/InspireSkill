"""Regression test for `inspire update` installer detection.

Background: a v3.0.1 user reported that `inspire update` refused to
auto-upgrade with "this build isn't managed by uv tool / pipx", even
though their install was a textbook `uv tool install` at
``~/.local/share/uv/tools/inspire-skill/``.

Root cause: the detector previously did
``Path(sys.executable).resolve()``. The ``.resolve()`` follows the
venv's ``bin/python`` symlink through to the underlying interpreter
binary — for uv tool installs that lives at
``~/.local/share/uv/python/cpython-3.x.x-.../bin/python3``, which has
"uv" in its parts but **not** "tools". Detection fell to None, the
auto-upgrade refused, the user had to reinstall manually.

Fix: probe ``sys.prefix`` (the venv root) directly. Don't resolve.

These tests pin the detector against the literal layouts that uv tool
and pipx use, so any future regression that re-introduces resolve() or
otherwise scrubs the venv segment will fail here.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from inspire.cli.commands.update import (
    _clean_legacy_skill_targets,
    _detect_installer,
    _ensure_global_playwright_runtime,
    _ensure_playwright_runtime,
    _release_entries_between,
    _is_local_requirement,
    _parse_uv_tool_list,
    ReleaseEntry,
    _scan_stale_skill_patterns,
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


def test_detect_harnesses_includes_antigravity_cursor_qoder_and_kimi_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = {
        "claude": tmp_path / ".claude",
        "antigravity": tmp_path / ".gemini",
        "cursor": tmp_path / ".cursor",
        "qoder": tmp_path / ".qoder",
        "kimi-code": tmp_path / ".kimi-code",
        "opencode": tmp_path / ".config" / "opencode",
    }
    roots["claude"].mkdir()
    roots["antigravity"].mkdir()
    roots["cursor"].mkdir()
    roots["qoder"].mkdir()
    roots["kimi-code"].mkdir()
    monkeypatch.setattr(update_module, "HARNESS_ROOTS", roots)

    assert update_module._detect_harnesses() == [
        "claude",
        "antigravity",
        "cursor",
        "qoder",
        "kimi-code",
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


def test_upgrade_cli_retries_pypi_network_errors_with_mirrors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], str | None]] = []

    def fake_run(cmd, check, env, text, stdout, stderr):
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
    monkeypatch.setattr(update_module.subprocess, "run", fake_run)

    assert _upgrade_cli(silent=True) is True
    assert calls == [
        (["uv", "tool", "install", "--force", "--refresh", "inspire-skill"], None),
        (
            ["uv", "tool", "install", "--force", "--refresh", "inspire-skill"],
            "https://pypi.tuna.tsinghua.edu.cn/simple",
        ),
    ]


def test_upgrade_cli_pins_known_target_version_for_uv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, check, env, text, stdout, stderr):
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

    def fake_run(cmd, check, env, text, stdout, stderr):
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

    def fake_run(cmd, check, env, text, stdout, stderr):
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

    def fake_run(cmd, check, env, text, stdout, stderr):
        calls.append((cmd, env.get("INSPIRE_SKIP_UPDATE_CHECK") if env else None))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(update_module.subprocess, "run", fake_run)

    assert _ensure_global_playwright_runtime(silent=True) is True
    assert calls == [
        (["/Users/zillionx/.local/bin/inspire", "_ensure-playwright-runtime", "--silent"], "1")
    ]


def test_global_runtime_setup_falls_back_when_hidden_hook_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback_calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        update_module,
        "_uv_tool_info",
        lambda: update_module.UvToolInfo(executable_path="/Users/zillionx/.local/bin/inspire"),
    )

    def fake_run(cmd, check, env, text, stdout, stderr):
        return subprocess.CompletedProcess(
            cmd,
            2,
            stdout="",
            stderr="Error: No such command '_ensure-playwright-runtime'.\n",
        )

    monkeypatch.setattr(update_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        update_module,
        "_ensure_playwright_runtime_with_wrapper_python",
        lambda executable, silent: fallback_calls.append((executable, silent)) or True,
    )

    assert _ensure_global_playwright_runtime(silent=True) is True
    assert fallback_calls == [("/Users/zillionx/.local/bin/inspire", True)]


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
        lambda silent, latest_version=None: calls.append("skills") or True,
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
    monkeypatch.setattr(
        "inspire.accounts.normalize_environment",
        lambda **_kwargs: calls.append("normalize"),
    )

    update_module.update.callback(
        check_only=False,
        silent=True,
        cli_only=False,
        skill_only=False,
    )

    assert calls == ["cli:4.1.1", "skills", "audit", "runtime", "normalize"]


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
        lambda **kwargs: calls.append(
            f"post:{kwargs['previous_version']}->{kwargs['expected_version']}"
        )
        or True,
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
    )

    assert calls == ["cli:5.2.3", "post:5.2.2->5.2.3"]


def test_clean_legacy_skill_targets_removes_old_antigravity_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = tmp_path / ".gemini" / "skills" / "inspire"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("# stale\n", encoding="utf-8")
    monkeypatch.setattr(update_module, "HARNESS_LEGACY_SKILL_DIRS", {"antigravity": [legacy]})

    assert _clean_legacy_skill_targets("antigravity", silent=True) is True
    assert not legacy.exists()


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
        "本文件同步 GitHub Releases 正文格式。\n\n"
        "# v5.2.3\n\n"
        "## 更新内容\n\n"
        "### 修复\n\n"
        "- 修复摘要兜底。\n\n"
        "# v5.2.2\n\n"
        "## 更新内容\n\n"
        "### 新增\n\n"
        "- 新增 Cursor。\n"
    )

    assert [(entry.tag, entry.body.strip()) for entry in entries] == [
        ("v5.2.3", "## 更新内容\n\n### 修复\n\n- 修复摘要兜底。"),
        ("v5.2.2", "## 更新内容\n\n### 新增\n\n- 新增 Cursor。"),
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


def test_update_prints_release_summary_after_successful_cli_upgrade(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    def fake_run_check(**kwargs):
        calls.append(f"check:{kwargs.get('current_version')}")
        if kwargs.get("current_version"):
            return {"current": kwargs.get("current_version"), "latest": "5.2.3"}
        return {"current": "5.2.1", "latest": "5.2.3"}

    monkeypatch.setattr(update_module, "run_check", fake_run_check)
    monkeypatch.setattr(update_module, "_print_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(update_module, "_upgrade_cli", lambda *args, **_kwargs: True)
    monkeypatch.setattr(update_module, "_refresh_skill_files", lambda *args, **_kwargs: True)
    monkeypatch.setattr(update_module, "_audit_update_state", lambda **_kwargs: (True, "5.2.3"))
    monkeypatch.setattr(update_module, "_ensure_global_playwright_runtime", lambda silent: True)
    monkeypatch.setattr(
        update_module,
        "_fetch_release_entries",
        lambda: [
            ReleaseEntry(tag="v5.2.3", body="## 更新内容\n\n### 新增\n\n- 新增 Cursor Harness 支持。"),
            ReleaseEntry(tag="v5.2.2", body="## 更新内容\n\n### 修复\n\n- 修复 Antigravity 安装目录。"),
            ReleaseEntry(tag="v5.2.1", body="## 更新内容\n\n### 新增\n\n- Qoder。"),
        ],
    )
    monkeypatch.setattr("inspire.accounts.normalize_environment", lambda **_kwargs: None)

    update_module.update.callback(
        check_only=False,
        silent=False,
        cli_only=False,
        skill_only=False,
    )

    output = capsys.readouterr().out
    assert "更新内容（v5.2.1 → v5.2.3）" in output
    assert "v5.2.3" in output
    assert "新增 Cursor Harness 支持" in output
    assert "v5.2.2" in output
    assert "修复 Antigravity 安装目录" in output


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


def test_stale_skill_patterns_detect_legacy_target_dir(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text(
        "Run INSPIRE_TARGET_DIR=/root/labwork inspire notebook exec ...\n",
        encoding="utf-8",
    )

    errors = _scan_stale_skill_patterns(tmp_path)

    assert errors
    assert "INSPIRE_TARGET_DIR" in errors[0]


def test_stale_skill_patterns_detect_low_level_playwright_repair(tmp_path: Path) -> None:
    (tmp_path / "references").mkdir()
    (tmp_path / "references" / "setup.md").write_text(
        "Repair with uvx --from inspire-skill playwright install chromium.\n",
        encoding="utf-8",
    )

    errors = _scan_stale_skill_patterns(tmp_path)

    assert errors
    assert "uvx --from inspire-skill playwright" in errors[0]


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

    def fake_run(cmd, check, env, text, stdout, stderr):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="inspire, version 4.1.1\n", stderr="")

    monkeypatch.setattr(update_module.subprocess, "run", fake_run)

    ok, actual = update_module._audit_global_cli(expected_version="4.1.1", silent=True)

    assert ok is True
    assert actual == "4.1.1"
    assert calls == [["/Users/zillionx/.local/bin/inspire", "--version"]]
