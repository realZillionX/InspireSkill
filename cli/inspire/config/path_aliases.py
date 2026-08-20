"""Remote path alias helpers for account-scoped project config and notebook commands."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from inspire.config.models import ConfigError
from inspire.config.toml import _load_toml, _project_config_write_path

_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
PATH_ALIASES_SECTION = "path_aliases"
DEFAULT_REMOTE_CWD_ALIAS = "me"
_PREFERRED_REMOTE_CWD_ALIASES = (
    DEFAULT_REMOTE_CWD_ALIAS,
    "ssd.me",
    "hdd.me",
    "qb-ilm.me",
    "qb-ilm2.me",
)


def normalize_path_alias_map(raw_value: Any) -> dict[str, str]:
    """Normalize a TOML ``[path_aliases]`` table into ``alias -> path``."""
    if not isinstance(raw_value, dict):
        return {}

    result: dict[str, str] = {}

    def _walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                child = str(child_key or "").strip()
                if not child:
                    continue
                next_prefix = f"{prefix}.{child}" if prefix else child
                _walk(next_prefix, child_value)
            return

        alias = prefix.strip()
        path = str(value or "").strip()
        if not alias or not path:
            return
        result[alias] = path

    _walk("", raw_value)
    return result


def validate_path_alias(alias: str) -> str:
    value = str(alias or "").strip()
    if not value:
        raise ConfigError("Path alias cannot be empty.")
    if value == "as":
        raise ConfigError("Path alias cannot be 'as'.")
    if not _ALIAS_RE.match(value):
        raise ConfigError(
            "Invalid path alias. Use letters, digits, '.', '_' or '-', "
            "and start with a letter or digit."
        )
    return value


def validate_remote_alias_path(path: str) -> str:
    value = str(path or "").strip()
    if not value:
        raise ConfigError("Remote path cannot be empty.")
    if not value.startswith("/"):
        raise ConfigError("Remote path aliases must point to an absolute remote path.")
    return value


def _join_remote_path(base: str, suffix: str) -> str:
    base = str(base or "").rstrip("/")
    suffix = str(suffix or "").strip()
    if not suffix:
        return base or "/"
    suffix = suffix.lstrip("/")
    if not suffix:
        return base or "/"
    return f"{base}/{suffix}"


def join_remote_path(base: str, *parts: str) -> str:
    """Join segments of a path on the *remote* filesystem.

    Always ``/``, never the local separator. Remote paths land inside shell
    commands that run on a Linux compute node, so building them with
    ``os.path.join`` produces ``C:\\...``-style separators the moment the CLI
    runs on Windows — a path the remote node cannot resolve and will not
    complain about in any way that points back here.
    """
    joined = str(base or "")
    for part in parts:
        joined = _join_remote_path(joined, part)
    return joined


def resolve_remote_path_alias(
    value: str,
    aliases: dict[str, str] | None,
    *,
    require_absolute_or_alias: bool = False,
) -> tuple[str, bool]:
    """Resolve exact, ``alias:child`` or ``alias/child`` remote path aliases.

    Returns ``(resolved_path, used_alias)``. Unknown relative values are
    preserved for SCP path handling unless ``require_absolute_or_alias`` is set.
    """
    text = str(value or "").strip()
    alias_map = aliases or {}
    if not text:
        return text, False

    if text in alias_map:
        return alias_map[text], True

    if ":" in text:
        alias, suffix = text.split(":", 1)
        alias = alias.strip()
        if alias in alias_map:
            return _join_remote_path(alias_map[alias], suffix), True
        if require_absolute_or_alias and alias:
            raise ConfigError(f"Unknown path alias '{alias}'.")

    for alias in sorted(alias_map, key=len, reverse=True):
        prefix = f"{alias}/"
        if text.startswith(prefix):
            return _join_remote_path(alias_map[alias], text[len(prefix):]), True

    if require_absolute_or_alias and not text.startswith("/"):
        raise ConfigError(
            f"Unknown path alias or relative remote path: '{text}'. "
            "Use an absolute path or define it with `inspire notebook path set`."
        )

    return text, False


def default_remote_cwd(aliases: dict[str, str] | None) -> str | None:
    """Return the default remote working directory from path aliases."""
    alias_map = aliases or {}
    for alias in _PREFERRED_REMOTE_CWD_ALIASES:
        value = str(alias_map.get(alias) or "").strip()
        if value:
            return value
    return None


def resolve_remote_cwd(*, cwd: str | None, aliases: dict[str, str]) -> str | None:
    raw = str(cwd or "").strip()
    if not raw:
        return default_remote_cwd(aliases)
    resolved, _ = resolve_remote_path_alias(
        raw,
        aliases,
        require_absolute_or_alias=True,
    )
    return resolved


def project_path_alias_config_path() -> Path:
    return _project_config_write_path()


def load_project_path_alias_data() -> tuple[Path, dict[str, Any]]:
    """Load the nearest project config used for path alias writes."""
    config_path = project_path_alias_config_path()
    if config_path.exists():
        return config_path, _load_toml(config_path)
    return config_path, {}


def load_project_path_aliases() -> tuple[Path, dict[str, str]]:
    """Return project-scoped remote path aliases from the nearest project config."""
    config_path, data = load_project_path_alias_data()
    return config_path, normalize_path_alias_map(data.get(PATH_ALIASES_SECTION, {}))


def _delete_alias_from_section(section: dict[str, Any], alias: str) -> bool:
    if alias in section:
        section.pop(alias, None)
        return True

    parts = alias.split(".")
    if len(parts) <= 1:
        return False

    parents: list[tuple[dict[str, Any], str]] = []
    current: Any = section
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return False
        parents.append((current, part))
        current = current.get(part)

    if not isinstance(current, dict) or parts[-1] not in current:
        return False
    current.pop(parts[-1], None)

    for parent, key in reversed(parents):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            parent.pop(key, None)
        else:
            break
    return True


def write_project_path_alias(*, alias: str, remote_path: str) -> Path:
    """Write one path alias to the nearest project config and return its path."""
    alias = validate_path_alias(alias)
    remote_path = validate_remote_alias_path(remote_path)

    config_path, data = load_project_path_alias_data()

    section = data.get(PATH_ALIASES_SECTION)
    if not isinstance(section, dict):
        section = {}
        data[PATH_ALIASES_SECTION] = section
    section[alias] = remote_path

    from inspire.cli.commands.init.toml_helpers import _toml_dumps

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_toml_dumps(data), encoding="utf-8")
    return config_path


def delete_project_path_alias(alias: str) -> Path:
    """Delete one path alias from the nearest project config."""
    alias = validate_path_alias(alias)
    config_path, data = load_project_path_alias_data()
    section = data.get(PATH_ALIASES_SECTION)
    if not isinstance(section, dict) or not _delete_alias_from_section(section, alias):
        _, aliases = load_project_path_aliases()
        available = ", ".join(sorted(aliases)) or "(none)"
        raise ConfigError(f"Unknown path alias: {alias!r}. Available: {available}")

    if not section:
        data.pop(PATH_ALIASES_SECTION, None)

    from inspire.cli.commands.init.toml_helpers import _toml_dumps

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_toml_dumps(data), encoding="utf-8")
    return config_path


__all__ = [
    "DEFAULT_REMOTE_CWD_ALIAS",
    "PATH_ALIASES_SECTION",
    "default_remote_cwd",
    "delete_project_path_alias",
    "load_project_path_alias_data",
    "load_project_path_aliases",
    "normalize_path_alias_map",
    "project_path_alias_config_path",
    "resolve_remote_cwd",
    "resolve_remote_path_alias",
    "validate_path_alias",
    "validate_remote_alias_path",
    "write_project_path_alias",
]
