"""Name-only project resolution helpers.

Project IDs are required by the platform API, but they are not a valid CLI
surface.  This module keeps the legacy configuration migration boundary in
one place: aliases and old catalog keys may still be read, while every live
lookup is performed by the current project name.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, TypeVar

from inspire.cli.utils.id_resolver import looks_like_platform_id
from inspire.config import Config, ConfigError

T = TypeVar("T")


def _casefold_lookup(mapping: Mapping[str, Any], value: str) -> tuple[str, Any] | None:
    needle = str(value or "").strip().casefold()
    if not needle:
        return None
    for key, item in mapping.items():
        if str(key or "").strip().casefold() == needle:
            return str(key), item
    return None


def _catalog_name(
    config: Config,
    *,
    requested: str,
    configured_value: str = "",
) -> str:
    """Recover a visible name from either current or legacy catalog layouts."""
    catalog = config.project_catalog or {}
    keys = {
        str(requested or "").strip().casefold(),
        str(configured_value or "").strip().casefold(),
    }
    for key, entry in catalog.items():
        if not isinstance(entry, Mapping):
            continue
        key_text = str(key or "").strip()
        entry_project_id = str(
            entry.get("project_id") or entry.get("id") or ""
        ).strip()
        if key_text.casefold() not in keys and entry_project_id.casefold() not in keys:
            continue
        name = str(entry.get("name") or "").strip()
        if name and not looks_like_platform_id(name):
            return name
    return ""


def project_name_candidates(config: Config, requested: str) -> tuple[str, ...]:
    """Return visible project-name candidates for a user value.

    ``Config.projects`` historically stored ``alias -> project_id``.  That
    format is accepted only as a migration input.  If the old ID can be
    mapped through the catalog, its current visible name is used; otherwise
    the alias is considered stale and is rejected instead of being sent to
    the platform as an ID.
    """
    raw = str(requested or "").strip()
    if not raw:
        raise ConfigError("--project is required.")
    if looks_like_platform_id(raw):
        raise ConfigError("--project takes a project name.")

    configured = _casefold_lookup(config.projects or {}, raw)
    candidates: list[str] = []
    if configured is not None:
        alias, raw_value = configured
        configured_value = str(raw_value or "").strip()
        if configured_value:
            if looks_like_platform_id(configured_value):
                catalog_name = _catalog_name(
                    config,
                    requested=alias,
                    configured_value=configured_value,
                )
                if not catalog_name:
                    raise ConfigError(
                        f"Configured project alias {alias!r} no longer resolves "
                        "to a project name; run `inspire init` to refresh it."
                    )
                candidates.append(catalog_name)
            else:
                candidates.append(configured_value)
        catalog_name = _catalog_name(
            config,
            requested=alias,
            configured_value=configured_value,
        )
        if catalog_name:
            candidates.append(catalog_name)
    else:
        catalog_name = _catalog_name(config, requested=raw)
        if catalog_name:
            candidates.append(catalog_name)
        candidates.append(raw)

    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        value = str(candidate or "").strip()
        folded = value.casefold()
        if not value or folded in seen or looks_like_platform_id(value):
            continue
        seen.add(folded)
        result.append(value)
    if not result:
        raise ConfigError(f"Unknown project name {raw!r}.")
    return tuple(result)


def resolve_project(
    config: Config,
    requested: str,
    projects: Iterable[T],
    *,
    name_getter: Callable[[T], str] = lambda item: str(getattr(item, "name", "") or ""),
    id_getter: Callable[[T], str] = lambda item: str(
        getattr(item, "project_id", "") or getattr(item, "id", "") or ""
    ),
) -> T:
    """Resolve a project object by visible name, never by its platform ID."""
    candidates = project_name_candidates(config, requested)
    candidate_names = {candidate.casefold() for candidate in candidates}
    matches: list[T] = []
    seen_ids: set[str] = set()
    available: set[str] = set()
    for project in projects:
        name = str(name_getter(project) or "").strip()
        if name:
            available.add(name)
        if name.casefold() not in candidate_names:
            continue
        project_id = str(id_getter(project) or "").strip()
        identity = project_id or f"object:{id(project)}"
        if identity in seen_ids:
            continue
        seen_ids.add(identity)
        matches.append(project)

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ConfigError(
            f"Project name {requested!r} is ambiguous in the selected workspace."
        )

    available_text = ", ".join(sorted(available, key=str.casefold))
    suffix = f" Available: {available_text}." if available_text else ""
    raise ConfigError(f"Unknown project name {requested!r}.{suffix}")


def resolve_project_id(
    config: Config,
    requested: str,
    projects: Iterable[T],
    *,
    name_getter: Callable[[T], str] = lambda item: str(getattr(item, "name", "") or ""),
    id_getter: Callable[[T], str] = lambda item: str(
        getattr(item, "project_id", "") or getattr(item, "id", "") or ""
    ),
) -> str:
    """Resolve a project name to an internal ID for one API request."""
    project = resolve_project(
        config,
        requested,
        projects,
        name_getter=name_getter,
        id_getter=id_getter,
    )
    project_id = str(id_getter(project) or "").strip()
    if not project_id:
        raise ConfigError(f"Project {name_getter(project)!r} has no platform record.")
    return project_id


def project_display_name(config: Config, requested: str | None) -> str:
    """Return a visible name for output without exposing legacy IDs."""
    if not requested:
        return "(project name unavailable)"
    try:
        return project_name_candidates(config, requested)[0]
    except ConfigError:
        return str(requested).strip() or "(project name unavailable)"


__all__ = [
    "project_display_name",
    "project_name_candidates",
    "resolve_project",
    "resolve_project_id",
]
