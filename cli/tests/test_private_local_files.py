"""Offline regressions for private creation and safe repair of old local state."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat
import sys
import threading

import pytest

from inspire import local_files
from inspire.accounts import storage
from inspire.config.toml import _load_toml


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX creation modes")
def test_private_file_is_never_observable_with_wide_permissions(monkeypatch, tmp_path):
    target = tmp_path / "config.toml"
    real_open = os.open
    real_replace = os.replace
    observations = []

    def observe_open(path, flags, mode=0o777, **kwargs):
        fd = real_open(path, flags, mode, **kwargs)
        if flags & os.O_CREAT and str(path).endswith(".tmp"):
            # This is the first instant the inode can be observed, before the
            # writer has received its descriptor or could chmod it.
            observations.append(stat.S_IMODE(os.fstat(fd).st_mode))
            assert mode == 0o600
            assert observations[-1] == 0o600
        return fd

    def observe_replace(source, destination):
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        assert Path(source).read_text(encoding="utf-8") == "fixture payload"
        real_replace(source, destination)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    monkeypatch.setattr(os, "open", observe_open)
    monkeypatch.setattr(os, "replace", observe_replace)
    previous = os.umask(0o002)
    try:
        local_files.atomic_write_text(target, "fixture payload", private=True)
    finally:
        os.umask(previous)
    assert observations == [0o600]
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission migration")
def test_read_repairs_existing_0664_account_config(isolated_home, caplog):
    path = isolated_home / ".inspire/accounts/demo/config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('[auth]\nusername = "fixture-user"\n')
    for parent in (path.parent, path.parent.parent, path.parent.parent.parent):
        parent.chmod(0o775)
    path.chmod(0o664)
    assert _load_toml(path)["auth"]["username"] == "fixture-user"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert all(
        stat.S_IMODE(parent.stat().st_mode) == 0o700
        for parent in (path.parent, path.parent.parent, path.parent.parent.parent)
    )
    assert not caplog.records


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission migration")
def test_migration_narrows_a_config_written_before_the_rule(isolated_home):
    """A directory turning private hides the config; its own mode must follow."""
    account = isolated_home / ".inspire/accounts/legacy"
    account.mkdir(parents=True)
    config = account / "config.toml"
    config.write_text("[auth]\n", encoding="utf-8")
    account.chmod(0o775)
    config.chmod(0o664)
    storage.ensure_inspire_home()
    assert stat.S_IMODE(account.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission migration")
def test_directory_migration_and_creation(isolated_home):
    existing = isolated_home / ".inspire/accounts/old"
    existing.mkdir(parents=True)
    existing.chmod(0o775)
    storage.ensure_inspire_home()
    assert stat.S_IMODE(existing.stat().st_mode) == 0o700
    created = storage.create_account("new", "fixture payload")
    assert stat.S_IMODE(created.stat().st_mode) == 0o700
    assert stat.S_IMODE((created / "config.toml").stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission migration")
@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_migration_never_widens_or_reports_unchanged_files(isolated_home, caplog, mode):
    path = isolated_home / ".inspire/config.toml"
    path.parent.mkdir()
    path.write_text("fixture payload")
    path.chmod(mode)
    local_files.repair_inspire_path(path)
    assert stat.S_IMODE(path.stat().st_mode) == mode
    assert not caplog.records


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink and chmod contract")
@pytest.mark.parametrize("link_at", ["home", "accounts", "account", "file"])
def test_migration_does_not_follow_symlinks(isolated_home, tmp_path, link_at):
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o775)
    outside_file = outside / "config.toml"
    outside_file.write_text("fixture payload")
    outside_file.chmod(0o664)
    root = isolated_home / ".inspire"
    if link_at == "home":
        root.symlink_to(outside, target_is_directory=True)
        target = root / "config.toml"
    elif link_at == "accounts":
        root.mkdir()
        (root / "accounts").symlink_to(outside, target_is_directory=True)
        target = root / "accounts/config.toml"
    elif link_at == "account":
        (root / "accounts").mkdir(parents=True)
        (root / "accounts/demo").symlink_to(outside, target_is_directory=True)
        target = root / "accounts/demo/config.toml"
    else:
        (root / "accounts/demo").mkdir(parents=True)
        target = root / "accounts/demo/config.toml"
        target.symlink_to(outside_file)
    local_files.repair_inspire_path(target)
    assert stat.S_IMODE(outside.stat().st_mode) == 0o775
    assert stat.S_IMODE(outside_file.stat().st_mode) == 0o664


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX repair failure")
def test_migration_failure_is_best_effort_and_warns_once(isolated_home, monkeypatch, caplog):
    path = isolated_home / ".inspire/config.toml"
    path.parent.mkdir(mode=0o700)
    path.write_text('[auth]\nusername = "fixture-user"\n')
    path.chmod(0o664)

    def fail(*args):
        raise PermissionError("fixture permission failure")

    monkeypatch.setattr(os, "fchmod", fail)
    for _ in range(2):
        assert _load_toml(path)["auth"]["username"] == "fixture-user"
    assert len(caplog.records) == 1
    assert str(path) in caplog.text
    assert "fixture permission failure" in caplog.text


def test_unlocked_concurrent_writers_have_independent_temporaries(monkeypatch, tmp_path):
    target = tmp_path / "current"
    real_replace = os.replace
    barrier = threading.Barrier(2)
    sources = []

    def replace(source, destination):
        if source not in sources:
            sources.append(source)
            barrier.wait(timeout=5)
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(local_files.atomic_write_text, target, value) for value in ("a", "b")
        ]
        for future in futures:
            future.result(timeout=10)
    assert len(set(sources)) == 2
    assert target.read_text(encoding="utf-8") in {"a", "b"}
    assert list(tmp_path.iterdir()) == [target]
