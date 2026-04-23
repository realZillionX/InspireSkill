"""Workspace selection utilities.

The CLI never guesses a workspace by GPU type or "CPU vs GPU" role. The
only ways to pick a workspace are:

1. ``--workspace <name-or-id>`` on the command itself → ``explicit_*``
2. ``[context].workspace = "<name-or-id>"`` in the repo's ``./.inspire/config.toml``
3. ``INSPIRE_WORKSPACE_ID`` env var

If none of those resolve to a real workspace, the call fails loudly.
``gpu_type`` / ``cpu_only`` / ``prefer_internet`` used to route to
``workspace_cpu_id`` / ``workspace_gpu_id`` / ``workspace_internet_id``
role slots — those no longer exist. The kwargs are kept on the signature
so existing callers don't break; they're silently ignored.
"""

from __future__ import annotations

import re
from typing import Optional

from inspire.config import Config, ConfigError

_WORKSPACE_ID_RE = re.compile(
    r"^ws-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_PLACEHOLDER_WORKSPACE_ID = "ws-00000000-0000-0000-0000-000000000000"


def _validate_workspace_id(value: str) -> None:
    if value == _PLACEHOLDER_WORKSPACE_ID:
        raise ConfigError(
            "workspace_id is set to the placeholder value. "
            "Configure a real workspace id in config.toml or set INSPIRE_WORKSPACE_ID."
        )
    if not _WORKSPACE_ID_RE.match(value):
        raise ConfigError(f"Invalid workspace_id format: {value!r}")


def select_workspace_id(
    config: Config,
    *,
    gpu_type: Optional[str] = None,  # ignored — see module docstring
    cpu_only: Optional[bool] = None,  # ignored
    prefer_internet: bool = False,  # ignored
    explicit_workspace_id: Optional[str] = None,
    explicit_workspace_name: Optional[str] = None,
) -> Optional[str]:
    """Resolve a workspace id from an explicit override or ``[context].workspace``.

    Precedence:
      1. ``explicit_workspace_id``
      2. ``explicit_workspace_name`` — looked up against ``[workspaces]``
      3. ``config.job_workspace_id`` (from ``[context].workspace`` or
         ``INSPIRE_WORKSPACE_ID``)
    """
    del gpu_type, cpu_only, prefer_internet  # no longer consulted

    if explicit_workspace_id:
        _validate_workspace_id(explicit_workspace_id)
        return explicit_workspace_id

    if explicit_workspace_name:
        key = explicit_workspace_name.strip()
        if not key:
            raise ConfigError("Workspace name cannot be empty")

        candidate: Optional[str] = None
        normalized = key.lower()
        for name, workspace_id in (config.workspaces or {}).items():
            if name.lower() == normalized:
                candidate = workspace_id
                break

        if not candidate:
            available = sorted((config.workspaces or {}).keys())
            available_hint = ", ".join(available) if available else "(none configured)"
            raise ConfigError(
                f"Unknown workspace name: {explicit_workspace_name!r}. "
                f"Configure it under [workspaces] in config.toml. Available: {available_hint}"
            )

        _validate_workspace_id(candidate)
        return candidate

    candidate = config.job_workspace_id
    if candidate:
        _validate_workspace_id(candidate)
    return candidate
