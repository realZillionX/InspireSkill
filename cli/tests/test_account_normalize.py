"""Tests for local browser-runtime setup used by account creation."""
from __future__ import annotations

from pathlib import Path

import pytest

import inspire.accounts.normalize as normalize_module
from inspire.accounts import normalize_environment


@pytest.fixture(autouse=True)
def _stub_playwright_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: pretend playwright chromium is installed.

    Tests that exercise the missing-playwright branch override this.
    """
    monkeypatch.setattr(
        "inspire.accounts.normalize._playwright_chromium_available",
        lambda: True,
    )


def test_playwright_missing_no_auto_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "inspire.accounts.normalize._playwright_chromium_available",
        lambda: False,
    )
    install_called: list[bool] = []
    monkeypatch.setattr(
        "inspire.accounts.normalize._install_playwright_chromium",
        lambda *_a, **_k: install_called.append(True) or True,
    )

    report = normalize_environment(interactive=False, auto_install_playwright=False)
    assert report.playwright_ready is False
    assert report.playwright_install_attempted is False
    assert install_called == []


def test_missing_playwright_notice_uses_standard_update_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(normalize_module, "_playwright_chromium_available", lambda: False)

    normalize_environment(interactive=True, auto_install_playwright=False)

    stderr = capsys.readouterr().err
    assert "inspire update --cli-only" in stderr
    assert "uvx --from" not in stderr
    assert "python -m playwright" not in stderr


def test_playwright_missing_auto_install_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness = iter([False, True])
    monkeypatch.setattr(normalize_module, "_playwright_chromium_available", lambda: next(readiness))
    monkeypatch.setattr(
        "inspire.accounts.normalize._install_playwright_chromium",
        lambda *_a, **_k: True,
    )

    report = normalize_environment(interactive=True, auto_install_playwright=True)
    assert report.playwright_install_attempted is True
    assert report.playwright_install_succeeded is True
    assert report.playwright_ready is True


def test_playwright_binary_install_can_leave_runtime_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(normalize_module, "_playwright_chromium_available", lambda: False)
    monkeypatch.setattr(normalize_module, "_install_playwright_chromium", lambda *_a, **_k: True)

    report = normalize_environment(interactive=True, auto_install_playwright=True)
    assert report.playwright_install_attempted is True
    assert report.playwright_install_succeeded is True
    assert report.playwright_ready is False


def test_playwright_missing_auto_install_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "inspire.accounts.normalize._playwright_chromium_available",
        lambda: False,
    )
    monkeypatch.setattr(
        "inspire.accounts.normalize._install_playwright_chromium",
        lambda *_a, **_k: False,
    )

    report = normalize_environment(interactive=True, auto_install_playwright=True)
    assert report.playwright_install_attempted is True
    assert report.playwright_install_succeeded is False
    assert report.playwright_ready is False


def test_install_playwright_chromium_uses_shared_install_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        normalize_module,
        "playwright_install_args",
        lambda *, include_system_deps=None: [
            "install",
            "with-deps" if include_system_deps else "no-deps",
        ],
    )
    monkeypatch.setattr(normalize_module, "_current_environment_playwright_bin", lambda: None)
    monkeypatch.setattr(normalize_module.shutil, "which", lambda _name: None)

    def fake_run(cmd: list[str], **_kwargs) -> None:
        calls.append(cmd)

    monkeypatch.setattr(normalize_module.subprocess, "run", fake_run)

    assert normalize_module._install_playwright_chromium()
    assert calls == [
        [normalize_module.sys.executable, "-m", "playwright", "install", "no-deps"]
    ]


def test_install_playwright_chromium_prefers_current_environment_binary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "tool" / "bin"
    bin_dir.mkdir(parents=True)
    python = bin_dir / "python"
    playwright = bin_dir / "playwright"
    python.write_text("", encoding="utf-8")
    playwright.write_text("", encoding="utf-8")
    playwright.chmod(0o755)
    monkeypatch.setattr(normalize_module.sys, "executable", str(python))
    monkeypatch.setattr(normalize_module.shutil, "which", lambda _name: "/usr/local/bin/playwright")
    monkeypatch.setattr(
        normalize_module,
        "playwright_install_args",
        lambda *, include_system_deps=None: ["install", "chromium"],
    )
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs) -> None:
        calls.append(cmd)

    monkeypatch.setattr(normalize_module.subprocess, "run", fake_run)

    assert normalize_module._install_playwright_chromium()
    assert calls == [[str(playwright), "install", "chromium"]]
