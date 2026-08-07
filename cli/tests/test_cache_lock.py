from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from inspire.accounts.cache_lock import CacheLockTimeout, exclusive_cache_lock


@pytest.fixture
def held_lock(tmp_path: Path) -> Iterator[Path]:
    """Hold *cache_path*'s lock in a background thread for the test's duration."""
    cache_path = tmp_path / "notebook-targets.json"
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with exclusive_cache_lock(cache_path):
            holding.set()
            release.wait(timeout=30)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert holding.wait(timeout=10)
        yield cache_path
    finally:
        release.set()
        holder.join(timeout=10)


def test_uncontended_lock_does_not_announce_a_wait(tmp_path: Path) -> None:
    # Given: nothing else holds the lock.
    announced: list[str] = []

    # When: a caller that would explain a pause takes the lock.
    with exclusive_cache_lock(
        tmp_path / "notebook-targets.json",
        timeout=10.0,
        on_wait=lambda: announced.append("waiting"),
    ):
        pass

    # Then: the fast path stays silent.
    assert announced == []


def test_bounded_wait_gives_up_on_a_stuck_holder(held_lock: Path) -> None:
    # Given: another process is holding the lock and will not release it.
    announced: list[str] = []

    # When: a caller waits with a deadline instead of blocking forever.
    with pytest.raises(CacheLockTimeout):
        with exclusive_cache_lock(
            held_lock,
            timeout=0.2,
            on_wait=lambda: announced.append("waiting"),
        ):  # pragma: no cover - the body must never run
            pass

    # Then: the caller was told once about the wait, then released to decide.
    assert announced == ["waiting"]


def test_lock_is_reacquirable_after_the_holder_releases(tmp_path: Path) -> None:
    # Given: a lock that a previous caller already used and released.
    cache_path = tmp_path / "notebook-targets.json"
    with exclusive_cache_lock(cache_path):
        pass

    # When/Then: the next caller takes it without waiting.
    with exclusive_cache_lock(cache_path, timeout=0.2):
        pass
