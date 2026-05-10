"""Workload condition profile parsing and resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from inspire.config.models import CONFIG_FILENAME, PROJECT_CONFIG_DIR, ConfigError
from inspire.config.toml import _find_project_config, _load_toml

PROFILE_FIELDS = ("workspace", "project", "group", "image", "quota")
PROFILE_KINDS = ("notebook", "job", "hpc", "ray", "serving")


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_workload_profiles(raw: Any) -> dict[str, dict[str, dict[str, str]]]:
    """Normalize a raw TOML ``[profiles]`` table.

    The public shape is ``[profiles.<kind>.<name>]`` with only the five
    scheduling condition fields. Unknown kinds, malformed entries, and empty
    values are ignored so partially edited config files remain readable.
    """
    if not isinstance(raw, dict):
        return {}

    normalized: dict[str, dict[str, dict[str, str]]] = {}
    for kind, raw_profiles in raw.items():
        kind_name = str(kind).strip()
        if kind_name not in PROFILE_KINDS or not isinstance(raw_profiles, dict):
            continue

        kind_profiles: dict[str, dict[str, str]] = {}
        for alias, raw_profile in raw_profiles.items():
            alias_name = str(alias).strip()
            if not alias_name or not isinstance(raw_profile, dict):
                continue
            profile: dict[str, str] = {}
            for field in PROFILE_FIELDS:
                value = _clean_text(raw_profile.get(field))
                if value is not None:
                    profile[field] = value
            if profile:
                kind_profiles[alias_name] = profile
        if kind_profiles:
            normalized[kind_name] = kind_profiles
    return normalized


def merge_workload_profiles(
    base: dict[str, dict[str, dict[str, str]]],
    override: dict[str, dict[str, dict[str, str]]],
) -> dict[str, dict[str, dict[str, str]]]:
    merged = {kind: dict(profiles) for kind, profiles in (base or {}).items()}
    for kind, profiles in (override or {}).items():
        bucket = dict(merged.get(kind, {}))
        bucket.update(profiles)
        merged[kind] = bucket
    return merged


def project_profile_config_path() -> Path:
    """Return the project config path to use when writing profiles."""
    return _find_project_config() or (Path.cwd() / PROJECT_CONFIG_DIR / CONFIG_FILENAME)


def load_project_profile_data() -> tuple[Path, dict[str, Any]]:
    path = project_profile_config_path()
    data = _load_toml(path) if path.exists() else {}
    if not isinstance(data, dict):
        data = {}
    return path, data


def get_workload_profile(
    profiles: dict[str, dict[str, dict[str, str]]],
    kind: str,
    name: str | None,
) -> dict[str, str]:
    if not name:
        return {}
    bucket = profiles.get(kind) or {}
    if name in bucket:
        return dict(bucket[name])
    for alias, profile in bucket.items():
        if alias.lower() == name.lower():
            return dict(profile)
    available = ", ".join(sorted(bucket)) or "(none)"
    raise ConfigError(f"Unknown {kind} profile: {name!r}. Available: {available}")


def apply_workload_profile(
    *,
    profiles: dict[str, dict[str, dict[str, str]]],
    kind: str,
    profile_name: str | None,
    values: dict[str, Any],
) -> dict[str, str | None]:
    """Apply a workload profile under explicit values.

    Explicit command or batch fields win. Missing fields are filled from the
    named profile only when a profile is requested.
    """
    profile = get_workload_profile(profiles, kind, profile_name)
    merged: dict[str, str | None] = {}
    for field in PROFILE_FIELDS:
        explicit = _clean_text(values.get(field))
        merged[field] = explicit if explicit is not None else profile.get(field)
    return merged


def missing_profile_fields(values: dict[str, Any]) -> list[str]:
    return [field for field in PROFILE_FIELDS if _clean_text(values.get(field)) is None]


def profile_required_message(kind: str, field: str, *, batch: bool = False) -> str:
    flag = f"--{field}"
    if batch:
        return (
            f"Batch {kind} item is missing required condition field: {field}. "
            "Set it explicitly or set profile = \"<name>\"."
        )
    return (
        f"{flag} is required. Pass it explicitly or use "
        f"`inspire {kind} create --profile <name>`."
    )


__all__ = [
    "PROFILE_FIELDS",
    "PROFILE_KINDS",
    "apply_workload_profile",
    "get_workload_profile",
    "load_project_profile_data",
    "merge_workload_profiles",
    "missing_profile_fields",
    "normalize_workload_profiles",
    "profile_required_message",
    "project_profile_config_path",
]
