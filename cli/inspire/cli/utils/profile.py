"""Environment-only profile helpers for CLI defaults."""

from __future__ import annotations

import os
import re
from typing import Optional

PROFILE_ENV_MAP = {
    "WORKSPACE_ID": "INSPIRE_WORKSPACE_ID",
    "PROJECT_ID": "INSPIRE_PROJECT_ID",
    "TARGET_DIR": "INSPIRE_TARGET_DIR",
    "IMAGE": "INSP_IMAGE",
    "NOTEBOOK_IMAGE": "INSPIRE_NOTEBOOK_IMAGE",
    "NOTEBOOK_RESOURCE": "INSPIRE_NOTEBOOK_RESOURCE",
    "PRIORITY": "INSP_PRIORITY",
    "RTUNNEL_BIN": "INSPIRE_RTUNNEL_BIN",
}


def _normalize_profile_name(profile: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", profile.strip()).strip("_")
    return normalized.upper()


def apply_env_profile(profile: Optional[str]) -> Optional[str]:
    """Apply INSPIRE_PROFILE_<NAME>_* env defaults for the given profile."""
    if not profile:
        return None

    normalized = _normalize_profile_name(profile)
    if not normalized:
        return None

    prefix = f"INSPIRE_PROFILE_{normalized}_"
    for suffix, target_key in PROFILE_ENV_MAP.items():
        source_key = f"{prefix}{suffix}"
        if source_key in os.environ and not os.environ.get(target_key):
            os.environ[target_key] = os.environ[source_key]

    return normalized
