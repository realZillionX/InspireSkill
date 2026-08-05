"""Name-only project resolution helpers."""

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


def project_name_candidates(config: Config, requested: str) -> tuple[str, ...]:
    """Return the live project name represented by a name or configured alias."""
    raw = str(requested or "").strip()
    if not raw:
        raise ConfigError("--project is required.")
    if looks_like_platform_id(raw):
        raise ConfigError("--project takes a project name.")

    configured = _casefold_lookup(config.projects or {}, raw)
    if configured is not None:
        alias, configured_name = configured
        configured_name = str(configured_name or "").strip()
        if not configured_name:
            raise ConfigError(f"Configured project alias {alias!r} has no project name.")
        if looks_like_platform_id(configured_name):
            raise ConfigError(
                f"Configured project alias {alias!r} must map to a project name; "
                "run `inspire init` to refresh it."
            )
        return (configured_name,)
    return (raw,)


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
    """Return a visible project name for output."""
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
