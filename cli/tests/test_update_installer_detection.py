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

import pytest

from inspire.cli.commands.update import _detect_installer, _upgrade_cli

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
        (["uv", "tool", "upgrade", "inspire-skill"], None),
        (
            ["uv", "tool", "upgrade", "inspire-skill"],
            "https://pypi.tuna.tsinghua.edu.cn/simple",
        ),
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
