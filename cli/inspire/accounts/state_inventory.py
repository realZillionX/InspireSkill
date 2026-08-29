"""What this version of the CLI keeps, and what older releases left behind.

Every release that drops a state file leaves the old one on disk forever: the
code that read it is gone, so nothing ever deletes it. This module is the
single declaration of what the *current* version owns, so `inspire update` can
name everything else and offer to sweep it.

Adding a new state file means adding it here. A file that is written but not
declared will be reported as an orphan and offered for deletion, which is the
failure mode we want — noisy and reversible by re-running the command that
creates it, rather than silent accumulation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from inspire.accounts.storage import accounts_dir, inspire_home

# Written directly under ~/.inspire/.
_HOME_FILES = frozenset(
    {
        "current",  # active account name
        "notebook-targets.json",  # notebook name -> handle cache
        "notebook-gpu-models.json",  # per-notebook GPU probe result
        "update-status.json",  # last upstream version check
        "bridges.json",  # tunnel bridges when no account is active
    }
)

_HOME_DIRS = frozenset(
    {
        "accounts",
        "locks",  # notebook bootstrap locks when no account is active
        "metrics",  # plots written by `<workload> metrics --plot`
    }
)

# Written under ~/.inspire/accounts/<name>/.
_ACCOUNT_FILES = frozenset(
    {
        "config.toml",
        "web_session.json",
        "web_session.login-block.json",  # credentials CAS just rejected
        "resource-index.sqlite3",
        "notebook-ide-url.json",
        "bridges.json",
        "rtunnel-proxy-state.json",
    }
)

_ACCOUNT_DIRS = frozenset({"locks"})

# Owned directories whose contents belong to the user, not to a CLI version.
# `metrics/` holds plots someone explicitly asked for; a version bump is not a
# reason to delete them.
_USER_OUTPUT_DIRS = frozenset({"metrics"})

# Not ours and not the user's data. Deleting them accomplishes nothing because
# the OS recreates them, so they are neither owned nor swept.
_IGNORED_NAMES = frozenset({".DS_Store", ".localized", "Thumbs.db"})

# Repository-local ``.inspire`` was retired completely. Scan ordinary source
# locations under the user's home while avoiding dependency stores, VCS
# internals and OS-owned trees that can contain millions of unrelated files.
_REPOSITORY_SCAN_PRUNE_NAMES = frozenset(
    {
        ".cache",
        ".cargo",
        ".git",
        ".hg",
        ".local",
        ".rustup",
        ".svn",
        ".Trash",
        ".venv",
        "AppData",
        "Library",
        "node_modules",
        "__pycache__",
        "venv",
    }
)


@dataclass(frozen=True)
class OrphanEntry:
    """A path under ~/.inspire that no current code path reads or writes."""

    path: Path
    is_dir: bool

    @property
    def display(self) -> str:
        try:
            # as_posix so the whole string reads as one path: a `~/` prefix
            # spliced onto Windows' backslashes gives `~/.inspire\accounts`.
            return f"~/{self.path.relative_to(Path.home()).as_posix()}"
        except ValueError:
            return str(self.path)


def _is_lock_for(name: str, owned: frozenset[str]) -> bool:
    """Whether *name* is a lock file guarding an owned payload.

    Locks are created next to their payload as ``<name>.lock`` and, for the
    session refresh path, ``<name>.refresh.lock``. Deriving them beats listing
    every variant by hand and going stale the next time one is added.
    """
    if not name.endswith(".lock"):
        return False
    base = name[: -len(".lock")]
    return base in owned or base.rsplit(".", 1)[0] in owned


def _scan(directory: Path, owned_files: frozenset[str], owned_dirs: frozenset[str]) -> list[OrphanEntry]:
    if not directory.is_dir():
        return []

    orphans: list[OrphanEntry] = []
    for entry in sorted(directory.iterdir()):
        name = entry.name
        if name in _IGNORED_NAMES:
            continue
        if entry.is_dir():
            if name not in owned_dirs:
                orphans.append(OrphanEntry(path=entry, is_dir=True))
            continue
        if name in owned_files or _is_lock_for(name, owned_files):
            continue
        orphans.append(OrphanEntry(path=entry, is_dir=False))
    return orphans


def _find_retired_repository_state() -> list[OrphanEntry]:
    """Find repo-local ``.inspire`` directories without following symlinks."""
    user_home = Path.home()
    global_state = inspire_home()
    found: list[OrphanEntry] = []

    def _ignore_walk_error(_error: OSError) -> None:
        return

    for root, dir_names, _file_names in os.walk(
        user_home,
        topdown=True,
        onerror=_ignore_walk_error,
        followlinks=False,
    ):
        root_path = Path(root)
        retained: list[str] = []
        for name in sorted(dir_names):
            candidate = root_path / name
            if name == ".inspire":
                if candidate != global_state and not candidate.is_symlink():
                    found.append(OrphanEntry(path=candidate, is_dir=True))
                continue
            if name in _REPOSITORY_SCAN_PRUNE_NAMES or candidate.is_symlink():
                continue
            retained.append(name)
        dir_names[:] = retained
    return found


def find_orphan_state() -> list[OrphanEntry]:
    """Return obsolete account state and retired repo-local ``.inspire`` dirs.

    Contents of user-output directories are never reported: those are answers
    someone asked for, not version-bound state.
    """
    home = inspire_home()
    orphans = _scan(home, _HOME_FILES, _HOME_DIRS | _USER_OUTPUT_DIRS)

    accounts = accounts_dir()
    if accounts.is_dir():
        for account in sorted(accounts.iterdir()):
            if account.is_dir():
                orphans.extend(_scan(account, _ACCOUNT_FILES, _ACCOUNT_DIRS))
            elif account.name not in _IGNORED_NAMES:
                orphans.append(OrphanEntry(path=account, is_dir=False))

    orphans.extend(_find_retired_repository_state())
    return orphans


__all__ = ["OrphanEntry", "find_orphan_state"]
