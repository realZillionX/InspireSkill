"""Validation for explicit paths on Linux compute nodes."""

from __future__ import annotations

from inspire.config import ConfigError


def explicit_remote_cwd(value: str | None) -> str | None:
    """Return an explicit absolute remote cwd, or ``None`` when omitted."""
    path = str(value or "").strip()
    if not path:
        return None
    if not path.startswith("/"):
        raise ConfigError("--cwd must be an absolute remote path.")
    return path


__all__ = ["explicit_remote_cwd"]
