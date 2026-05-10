"""Tests for `inspire.accounts.normalize`.

`normalize_environment` runs once at `inspire account add` time (and again
as a free no-op at high-risk command entry points like `notebook ssh`).
These tests pin its behaviour so the rest of the CLI can keep assuming a
clean v3.x layout — no scattered ``if old_format`` guards.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from inspire.accounts import normalize_environment, NORMALIZATION_SENTINEL
from inspire.accounts.normalize import _LEGACY_FILES_UNDER_INSPIRE_HOME


def _isolate_inspire_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point both `inspire_home` and `~/.cache` at temp paths."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    inspire_home = fake_home / ".inspire"
    inspire_home.mkdir()
    return inspire_home


@pytest.fixture(autouse=True)
def _stub_playwright_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: pretend playwright chromium is installed.

    Tests that exercise the missing-playwright branch override this.
    """
    monkeypatch.setattr(
        "inspire.accounts.normalize._playwright_chromium_available",
        lambda: True,
    )


def test_quarantines_all_known_legacy_files_under_inspire_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    inspire_home = _isolate_inspire_home(monkeypatch, tmp_path)
    for filename, _ in _LEGACY_FILES_UNDER_INSPIRE_HOME:
        (inspire_home / filename).write_text("legacy", encoding="utf-8")

    report = normalize_environment()

    assert {p[0].name for p in report.quarantined} == {
        f for f, _ in _LEGACY_FILES_UNDER_INSPIRE_HOME
    }
    for filename, _ in _LEGACY_FILES_UNDER_INSPIRE_HOME:
        assert not (inspire_home / filename).exists()
        assert (inspire_home / f"{filename}.legacy").read_text(encoding="utf-8") == "legacy"
    assert (inspire_home / NORMALIZATION_SENTINEL).exists()


def test_quarantines_legacy_rtunnel_state_under_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    inspire_home = fake_home / ".inspire"
    inspire_home.mkdir()
    cache = fake_home / ".cache" / "inspire-skill"
    cache.mkdir(parents=True)
    (cache / "rtunnel-proxy-state.json").write_text("legacy", encoding="utf-8")

    report = normalize_environment()

    quarantined_names = {p[0].name for p in report.quarantined}
    assert "rtunnel-proxy-state.json" in quarantined_names
    assert not (cache / "rtunnel-proxy-state.json").exists()
    assert (cache / "rtunnel-proxy-state.json.legacy").exists()


def test_idempotent_via_sentinel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    inspire_home = _isolate_inspire_home(monkeypatch, tmp_path)
    (inspire_home / "bridges.json").write_text("legacy", encoding="utf-8")

    first = normalize_environment()
    assert first.quarantined  # first run quarantined
    assert (inspire_home / "bridges.json.legacy").exists()

    # Re-create a fresh bridges.json under the unscoped path; second run
    # must NOT touch it because the sentinel is set.
    (inspire_home / "bridges.json").write_text("rebuilt", encoding="utf-8")
    second = normalize_environment()
    assert second.quarantined == []
    assert (inspire_home / "bridges.json").read_text(encoding="utf-8") == "rebuilt"


def test_no_legacy_files_no_quarantine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    inspire_home = _isolate_inspire_home(monkeypatch, tmp_path)

    report = normalize_environment()

    assert report.quarantined == []
    # Sentinel is still written so subsequent runs short-circuit cheaply.
    assert (inspire_home / NORMALIZATION_SENTINEL).exists()


def test_workspace_env_var_is_not_special_cased(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_inspire_home(monkeypatch, tmp_path)
    monkeypatch.setenv("INSPIRE_WORKSPACE_ID", "ws-xxxxxx")

    report = normalize_environment()
    assert report.stale_env_vars == []


def test_stale_env_var_unset_not_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_inspire_home(monkeypatch, tmp_path)
    monkeypatch.delenv("INSPIRE_WORKSPACE_ID", raising=False)

    report = normalize_environment()
    assert report.stale_env_vars == []


def test_playwright_missing_no_auto_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_inspire_home(monkeypatch, tmp_path)
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


def test_playwright_missing_auto_install_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_inspire_home(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "inspire.accounts.normalize._playwright_chromium_available",
        lambda: False,
    )
    monkeypatch.setattr(
        "inspire.accounts.normalize._install_playwright_chromium",
        lambda *_a, **_k: True,
    )

    report = normalize_environment(interactive=True, auto_install_playwright=True)
    assert report.playwright_install_attempted is True
    assert report.playwright_install_succeeded is True
    assert report.playwright_ready is True


def test_playwright_missing_auto_install_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_inspire_home(monkeypatch, tmp_path)
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
