"""State files older versions left behind, and the sweep that removes them."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from inspire.accounts import state_inventory

# `inspire.cli.commands.update` the name resolves to the Click command, not the
# module — the command group rebinds it on import.
update_module = importlib.import_module("inspire.cli.commands.update")

# Captured at import, before conftest's autouse stub can replace the attribute.
_real_find_orphan_state = state_inventory.find_orphan_state


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A fake ~/.inspire holding one of everything this version owns."""
    fake_home = tmp_path / "__home"
    inspire = fake_home / ".inspire"
    account = inspire / "accounts" / "primary"
    account.mkdir(parents=True)
    (inspire / "metrics").mkdir()
    (inspire / "locks").mkdir()
    (account / "locks").mkdir()

    for name in (
        "current",
        "notebook-targets.json",
        "notebook-targets.json.lock",
        "notebook-gpu-models.json",
        "update-status.json",
        "bridges.json",
        ".DS_Store",
    ):
        (inspire / name).write_text("", encoding="utf-8")
    for name in (
        "config.toml",
        "web_session.json",
        "web_session.json.lock",
        "web_session.json.refresh.lock",
        "resource-index.sqlite3",
        "notebook-ide-url.json",
        "bridges.json",
        "rtunnel-proxy-state.json",
    ):
        (account / name).write_text("", encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: fake_home)
    # Undo conftest's blanket stub; these tests are about the real scan.
    monkeypatch.setattr(state_inventory, "find_orphan_state", _real_find_orphan_state)
    return inspire


def test_a_fully_owned_home_reports_nothing(home: Path) -> None:
    assert state_inventory.find_orphan_state() == []


def test_user_plots_are_never_reported(home: Path) -> None:
    """`metrics/` holds answers someone asked for, not version-bound state."""
    (home / "metrics" / "job-run-1.png").write_bytes(b"")

    assert state_inventory.find_orphan_state() == []


def test_unknown_files_and_dirs_are_reported(home: Path) -> None:
    (home / "jobs.json.legacy").write_text("", encoding="utf-8")
    (home / ".environment-normalized-v3").write_text("", encoding="utf-8")
    (home / "events").mkdir()
    (home / "events" / "a.events.json").write_text("", encoding="utf-8")
    (home / "accounts" / "primary" / "project_list.json").write_text("", encoding="utf-8")
    (home / "accounts" / "primary" / "resource-index-refresh.stamp").write_text(
        "", encoding="utf-8"
    )
    (home / "accounts" / "primary" / "config.toml.bak-7897").write_text("", encoding="utf-8")

    found = {entry.path.name: entry.is_dir for entry in state_inventory.find_orphan_state()}

    assert found == {
        "jobs.json.legacy": False,
        ".environment-normalized-v3": False,
        "events": True,
        "project_list.json": False,
        "resource-index-refresh.stamp": False,
        "config.toml.bak-7897": False,
    }


def test_sweep_keeps_everything_without_consent(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No TTY and no --yes means report only; nothing is deleted."""
    orphan = home / "jobs.json.legacy"
    orphan.write_text("", encoding="utf-8")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    result = update_module._sweep_orphan_state(
        silent=False, assume_yes=False, json_output=False
    )

    assert result == {"found": ["~/.inspire/jobs.json.legacy"], "removed": 0}
    assert orphan.exists()


def test_sweep_removes_files_and_directories_with_yes(home: Path) -> None:
    (home / "jobs.json.legacy").write_text("", encoding="utf-8")
    events = home / "events"
    events.mkdir()
    (events / "a.events.json").write_text("", encoding="utf-8")

    result = update_module._sweep_orphan_state(
        silent=False, assume_yes=True, json_output=False
    )

    assert result == {"removed": 2, "found": ["~/.inspire/events/", "~/.inspire/jobs.json.legacy"]}
    assert not events.exists()
    assert not (home / "jobs.json.legacy").exists()
    # Owned state survives the sweep.
    assert (home / "current").exists()
    assert (home / "accounts" / "primary" / "config.toml").exists()


def test_sweep_is_silent_for_the_background_check(home: Path) -> None:
    """The daily check runs unattended; it must never prompt or delete."""
    orphan = home / "jobs.json.legacy"
    orphan.write_text("", encoding="utf-8")

    assert (
        update_module._sweep_orphan_state(
            silent=True, assume_yes=False, json_output=False
        )
        is None
    )
    assert orphan.exists()


def test_sweep_never_breaks_a_successful_upgrade(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scan loads a module this process was not built against.

    By the time the sweep runs, the package on disk is the incoming release.
    If that module cannot load here, the upgrade already succeeded and must
    still report success.
    """

    def _boom() -> list:
        raise ImportError("new release restructured the package")

    monkeypatch.setattr(state_inventory, "find_orphan_state", _boom)

    assert (
        update_module._sweep_orphan_state(
            silent=False, assume_yes=True, json_output=False
        )
        is None
    )


def test_sweep_reports_without_deleting_in_json_mode(home: Path) -> None:
    """JSON mode has no operator to confirm, so it reports and keeps."""
    orphan = home / "jobs.json.legacy"
    orphan.write_text("", encoding="utf-8")

    result = update_module._sweep_orphan_state(
        silent=False, assume_yes=False, json_output=True
    )

    assert result == {"found": ["~/.inspire/jobs.json.legacy"], "removed": 0}
    assert orphan.exists()
