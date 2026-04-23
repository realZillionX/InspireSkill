"""One-shot migration of legacy config/cache artefacts into per-account dirs.

The CLI used to spread a user's state across three roots:

    ~/.config/inspire/config.toml           # [auth] + [accounts."<user>"] catalog
    ~/.inspire/bridges-<user>.json          # SSH tunnel aliases per platform user
    ~/.cache/inspire-skill/web_session-<user>.json   # SSO session cache

The new layout puts everything an account needs into one directory::

    ~/.inspire/accounts/<name>/config.toml
    ~/.inspire/accounts/<name>/bridges.json
    ~/.inspire/accounts/<name>/web_session.json

``inspire account migrate`` rewrites all three sources into that layout,
chooses an active account (via ``~/.inspire/current``), and backs up every
original file into ``~/.inspire/legacy-<timestamp>/`` so the user can revert.

Design notes:

* Migration is **idempotent in intent, non-destructive in practice** —
  originals are copied to the backup directory before removal, and target
  accounts are rejected with a friendly error if they already exist.
* Uses :func:`inspire.config.load_accounts._parse_global_accounts` so the
  catalog parser shipped with the loader is the single source of truth for
  how legacy per-account overrides are interpreted.
* The per-account TOML writer is deliberately small: the migration only
  needs to emit shapes that already appear in the legacy schema — scalars,
  simple tables, nested tables (``[project_catalog."<id>"]``), and arrays
  of tables (``[[compute_groups]]``). No library dependency required.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from inspire.accounts import storage
from inspire.config.load_accounts import _parse_global_accounts
from inspire.config.schema import CONFIG_OPTIONS
from inspire.config.toml import _load_toml

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Fields set via [accounts."<user>"].overrides need a round-trip from field
# name back to their original ``[section].key`` slot. Computed lazily from the
# shared CONFIG_OPTIONS registry so the migrator cannot drift from the loader.
_FIELD_TO_TOML_PATH: dict[str, tuple[str, str]] | None = None


def _field_to_toml_path(field_name: str) -> tuple[str, str] | None:
    global _FIELD_TO_TOML_PATH
    if _FIELD_TO_TOML_PATH is None:
        mapping: dict[str, tuple[str, str]] = {}
        for opt in CONFIG_OPTIONS:
            parts = opt.toml_key.split(".")
            if len(parts) == 2:
                mapping[opt.field_name] = (parts[0], parts[1])
            else:
                mapping[opt.field_name] = ("", parts[0])
        _FIELD_TO_TOML_PATH = mapping
    return _FIELD_TO_TOML_PATH.get(field_name)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class AccountPlan:
    """One account's migration: what to write and what to move."""

    name: str
    legacy_name: str  # the original (possibly non-sanitised) key used on disk
    config_toml: str
    bridges_source: Optional[Path] = None
    web_session_source: Optional[Path] = None


@dataclass
class MigrationPlan:
    """Aggregate plan for the whole host."""

    accounts: dict[str, AccountPlan] = field(default_factory=dict)
    active_account: Optional[str] = None
    backup_files: list[Path] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.accounts and not self.backup_files


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _legacy_global_path() -> Path:
    override = (os.getenv("INSPIRE_GLOBAL_CONFIG_PATH") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "inspire" / "config.toml"


def _legacy_inspire_dir() -> Path:
    return Path.home() / ".inspire"


def _legacy_cache_dir() -> Path:
    return Path.home() / ".cache" / "inspire-skill"


def _sanitize_to_account_name(raw: str) -> Optional[str]:
    """Best-effort conversion of a legacy identifier into a valid account name."""
    if not raw:
        return None
    try:
        return storage.validate_name(raw)
    except storage.AccountError:
        pass
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._")
    if not cleaned:
        return None
    try:
        return storage.validate_name(cleaned)
    except storage.AccountError:
        return None


def _original_name_for(
    account: str,
    *,
    passwords: dict[str, str],
    top_username: str,
) -> str:
    """Recover the pre-sanitised original name (preserves casing & legacy chars)."""
    for key in passwords:
        if _sanitize_to_account_name(key) == account:
            return key
    if top_username and _sanitize_to_account_name(top_username) == account:
        return top_username
    return account


# ---------------------------------------------------------------------------
# TOML dump (small but honest serializer for the per-account schema)
# ---------------------------------------------------------------------------


def _escape_key(k: str) -> str:
    if _BARE_KEY_RE.match(k):
        return k
    return '"' + k.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _dump_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return (
            '"'
            + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            + '"'
        )
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_dump_scalar(x) for x in v) + "]"
    raise TypeError(f"unsupported TOML value type: {type(v).__name__}")


def _is_table(v: Any) -> bool:
    return isinstance(v, dict)


def _is_array_of_tables(v: Any) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def _dump_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []

    top_scalars = [
        (k, v) for k, v in data.items() if not _is_table(v) and not _is_array_of_tables(v)
    ]
    for key, value in top_scalars:
        lines.append(f"{_escape_key(key)} = {_dump_scalar(value)}")

    top_tables = [(k, v) for k, v in data.items() if _is_table(v)]
    for key, value in top_tables:
        _emit_table(lines, [key], value)

    top_arrays = [(k, v) for k, v in data.items() if _is_array_of_tables(v)]
    for key, items in top_arrays:
        for item in items:
            lines.append("")
            lines.append(f"[[{_escape_key(key)}]]")
            for ik, iv in item.items():
                if _is_table(iv) or _is_array_of_tables(iv):
                    continue  # our schema does not use nested tables here
                lines.append(f"{_escape_key(ik)} = {_dump_scalar(iv)}")

    text = "\n".join(lines).strip() + "\n"
    return text


def _emit_table(lines: list[str], path: list[str], data: dict[str, Any]) -> None:
    scalar_items = [
        (k, v) for k, v in data.items() if not _is_table(v) and not _is_array_of_tables(v)
    ]
    table_items = [(k, v) for k, v in data.items() if _is_table(v)]

    if scalar_items or not table_items:
        lines.append("")
        lines.append(f"[{'.'.join(_escape_key(p) for p in path)}]")
        for k, v in scalar_items:
            lines.append(f"{_escape_key(k)} = {_dump_scalar(v)}")

    for k, v in table_items:
        _emit_table(lines, path + [k], v)


# ---------------------------------------------------------------------------
# Per-account rendering
# ---------------------------------------------------------------------------


def _render_account_config(
    *,
    legacy_name: str,
    legacy_raw: dict[str, Any],
    passwords: dict[str, str],
    catalogs: dict[str, dict[str, Any]],
) -> str:
    """Produce the new per-account ``config.toml`` body.

    The base is the legacy global TOML minus structural noise
    (``[accounts]``, ``[context]``, ``[cli]``). Per-account overrides then
    get applied on top at the right TOML slots.
    """
    out: dict[str, Any] = {}
    for k, v in legacy_raw.items():
        if k in {"accounts", "context", "cli"}:
            continue
        out[k] = copy.deepcopy(v)

    # Inject identity
    auth = out.setdefault("auth", {})
    if isinstance(auth, dict):
        auth["username"] = legacy_name
        if legacy_name in passwords and passwords[legacy_name]:
            auth["password"] = passwords[legacy_name]

    catalog = catalogs.get(legacy_name) or {}

    # Merge dict-shape overrides with top-level counterparts
    for section in ("workspaces", "projects"):
        override = catalog.get(section)
        if isinstance(override, dict) and override:
            merged = dict(out.get(section) or {})
            merged.update({str(k): str(v) for k, v in override.items()})
            out[section] = merged

    # project_catalog: map of project_id → {shared_path_group, workdir}
    pc_override = catalog.get("project_catalog")
    if isinstance(pc_override, dict) and pc_override:
        merged_pc = dict(out.get("project_catalog") or {})
        for proj_id, entry in pc_override.items():
            if isinstance(entry, dict):
                merged_pc[str(proj_id)] = {
                    k: v for k, v in entry.items() if v not in (None, "")
                }
        if merged_pc:
            out["project_catalog"] = merged_pc

    # compute_groups: if the account has its own list, that takes precedence
    cg_override = catalog.get("compute_groups")
    if isinstance(cg_override, list) and cg_override:
        out["compute_groups"] = cg_override

    # Flat scalar overrides → reverse-map to their schema TOML slots
    for field_name, value in (catalog.get("overrides") or {}).items():
        path = _field_to_toml_path(field_name)
        if path is None:
            continue
        section, key = path
        if section:
            section_dict = out.setdefault(section, {})
            if isinstance(section_dict, dict):
                section_dict[key] = value
        else:
            out[key] = value

    # account-level shared_path_group / train_job_workdir live under [account]
    shared_pg = catalog.get("shared_path_group")
    train_workdir = catalog.get("train_job_workdir")
    if shared_pg or train_workdir:
        account_block = out.setdefault("account", {})
        if isinstance(account_block, dict):
            if shared_pg:
                account_block["shared_path_group"] = str(shared_pg)
            if train_workdir:
                account_block["train_job_workdir"] = str(train_workdir)

    return _dump_toml(out)


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------


def _find_bridges_for(legacy_name: str, inspire_dir: Path) -> Optional[Path]:
    p = inspire_dir / f"bridges-{legacy_name}.json"
    return p if p.exists() else None


def _find_web_session_for(legacy_name: str, cache_dir: Path) -> Optional[Path]:
    p = cache_dir / f"web_session-{legacy_name}.json"
    return p if p.exists() else None


def build_plan() -> MigrationPlan:
    """Scan disk and produce a concrete migration plan. Read-only."""
    plan = MigrationPlan()
    inspire_dir = _legacy_inspire_dir()
    cache_dir = _legacy_cache_dir()

    legacy_path = _legacy_global_path()
    legacy_raw: dict[str, Any] = {}
    if legacy_path.exists():
        try:
            legacy_raw = _load_toml(legacy_path)
        except Exception:
            legacy_raw = {}
        plan.backup_files.append(legacy_path)

    passwords, catalogs = _parse_global_accounts(legacy_raw.get("accounts", {}))

    raw_auth = legacy_raw.get("auth")
    top_username = ""
    if isinstance(raw_auth, dict):
        top_username = str(raw_auth.get("username") or "").strip()
    if not top_username:
        top_username = str(legacy_raw.get("username") or "").strip()

    # Collect account names from every source
    name_to_legacy: dict[str, str] = {}

    def _register(raw_name: str) -> None:
        sanitised = _sanitize_to_account_name(raw_name)
        if sanitised and sanitised not in name_to_legacy:
            name_to_legacy[sanitised] = raw_name

    for raw_name in passwords:
        _register(raw_name)
    for raw_name in catalogs:
        _register(raw_name)
    if top_username:
        _register(top_username)

    if inspire_dir.exists():
        for p in inspire_dir.glob("bridges-*.json"):
            m = re.match(r"bridges-(.+)\.json$", p.name)
            if m:
                _register(m.group(1))
            plan.backup_files.append(p)
        unscoped = inspire_dir / "bridges.json"
        if unscoped.exists():
            plan.backup_files.append(unscoped)

    if cache_dir.exists():
        for p in cache_dir.glob("web_session-*.json"):
            m = re.match(r"web_session-(.+)\.json$", p.name)
            if m:
                _register(m.group(1))
            plan.backup_files.append(p)
        unscoped = cache_dir / "web_session.json"
        if unscoped.exists():
            plan.backup_files.append(unscoped)

    for sanitised in sorted(name_to_legacy):
        legacy_name = name_to_legacy[sanitised]
        config_toml = _render_account_config(
            legacy_name=legacy_name,
            legacy_raw=legacy_raw,
            passwords=passwords,
            catalogs=catalogs,
        )
        plan.accounts[sanitised] = AccountPlan(
            name=sanitised,
            legacy_name=legacy_name,
            config_toml=config_toml,
            bridges_source=_find_bridges_for(legacy_name, inspire_dir)
            if inspire_dir.exists()
            else None,
            web_session_source=_find_web_session_for(legacy_name, cache_dir)
            if cache_dir.exists()
            else None,
        )

    # Active account resolution
    raw_context = legacy_raw.get("context") or {}
    if isinstance(raw_context, dict):
        ctx_account = str(raw_context.get("account") or "").strip()
    else:
        ctx_account = ""
    if ctx_account:
        sanitised = _sanitize_to_account_name(ctx_account)
        if sanitised and sanitised in plan.accounts:
            plan.active_account = sanitised
    if not plan.active_account and top_username:
        sanitised = _sanitize_to_account_name(top_username)
        if sanitised and sanitised in plan.accounts:
            plan.active_account = sanitised
    if not plan.active_account and len(plan.accounts) == 1:
        plan.active_account = next(iter(plan.accounts))

    return plan


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


class MigrationConflictError(RuntimeError):
    """Raised when a target account directory already exists."""


def execute_plan(plan: MigrationPlan) -> Path:
    """Apply a plan. Returns the backup directory path.

    Refuses (``MigrationConflictError``) if any target account dir already
    exists. Creates the backup dir, copies every file in ``plan.backup_files``
    there, then writes the new account dirs and moves the bridge/session
    files into them. Finally removes any remaining originals and sets
    ``~/.inspire/current`` to the chosen active account.
    """
    conflicts = [n for n in plan.accounts if storage.account_exists(n)]
    if conflicts:
        raise MigrationConflictError(
            "Target account(s) already exist; refusing to overwrite: "
            + ", ".join(sorted(conflicts))
        )

    storage.ensure_inspire_home()
    backup_dir = storage.inspire_home() / f"legacy-{int(time.time())}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Copy originals to backup before any destructive ops
    for source in plan.backup_files:
        if not source.exists():
            continue
        shutil.copy2(source, backup_dir / source.name)

    # Write new account dirs; move bridges and sessions into place
    for name, acct in plan.accounts.items():
        acct_path = storage.account_dir(name)
        acct_path.mkdir(parents=True, exist_ok=True)
        (acct_path / "config.toml").write_text(acct.config_toml, encoding="utf-8")
        if acct.bridges_source and acct.bridges_source.exists():
            shutil.move(str(acct.bridges_source), str(acct_path / "bridges.json"))
        if acct.web_session_source and acct.web_session_source.exists():
            shutil.move(
                str(acct.web_session_source), str(acct_path / "web_session.json")
            )

    # Remove any remaining originals (they've been backed up)
    for source in plan.backup_files:
        if source.exists():
            try:
                source.unlink()
            except OSError:
                continue

    if plan.active_account:
        storage.set_current_account(plan.active_account)

    return backup_dir


# ---------------------------------------------------------------------------
# Public helpers for the CLI
# ---------------------------------------------------------------------------


def describe_plan(plan: MigrationPlan) -> list[str]:
    """Produce human-readable plan summary lines."""
    out: list[str] = []
    if plan.is_empty:
        out.append("Nothing to migrate — no legacy config, bridges, or session files found.")
        return out

    home = storage.inspire_home()
    out.append(f"Will create the following accounts under {home}/accounts/:")
    for name, acct in plan.accounts.items():
        marker = " (active)" if name == plan.active_account else ""
        out.append(f"  * {name}{marker}  — source login: {acct.legacy_name}")
        details: list[str] = []
        if acct.bridges_source:
            details.append(f"bridges from {acct.bridges_source.name}")
        if acct.web_session_source:
            details.append(f"session from {acct.web_session_source.name}")
        if details:
            out.append("      " + "; ".join(details))

    out.append("")
    out.append("Legacy files to back up and remove:")
    for source in plan.backup_files:
        out.append(f"  - {source}")
    out.append("")
    out.append(
        f"Backup location: {home}/legacy-<timestamp>/ "
        "(created just before the move — safe to delete later)."
    )
    return out


__all__ = [
    "AccountPlan",
    "MigrationConflictError",
    "MigrationPlan",
    "build_plan",
    "describe_plan",
    "execute_plan",
]
