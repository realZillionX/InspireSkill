"""Workspace selection utilities.

The Inspire platform separates resources by workspace. For convenience, the CLI can
auto-select a workspace based on requested resources.
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
    gpu_type: Optional[str] = None,
    cpu_only: Optional[bool] = None,
    prefer_internet: bool = False,  # deprecated, ignored — kept for caller compat
    explicit_workspace_id: Optional[str] = None,
    explicit_workspace_name: Optional[str] = None,
) -> Optional[str]:
    """Select a workspace_id based on requested resource type.

    Precedence:
      1) explicit_workspace_id
      2) explicit_workspace_name — 'cpu' / 'gpu' shortcuts or any alias
         from the account's ``[workspaces]`` map
      3) Routed workspaces.cpu / workspaces.gpu
      4) Legacy job_workspace_id (job.workspace_id / INSPIRE_WORKSPACE_ID)
    """
    del prefer_internet  # no longer honored — see module docstring

    if explicit_workspace_id:
        _validate_workspace_id(explicit_workspace_id)
        return explicit_workspace_id

    if explicit_workspace_name:
        key = explicit_workspace_name.strip()
        if not key:
            raise ConfigError("Workspace name cannot be empty")

        normalized = key.lower()
        if normalized in {"cpu", "default"}:
            candidate = config.workspace_cpu_id or config.job_workspace_id
            if not candidate:
                raise ConfigError(
                    "No CPU workspace configured. Set [workspaces].cpu or INSPIRE_WORKSPACE_ID."
                )
            _validate_workspace_id(candidate)
            return candidate

        if normalized == "gpu":
            candidate = config.workspace_gpu_id or config.job_workspace_id
            if not candidate:
                raise ConfigError(
                    "No GPU workspace configured. Set [workspaces].gpu or INSPIRE_WORKSPACE_ID."
                )
            _validate_workspace_id(candidate)
            return candidate

        candidate = None
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

    # CPU requests (or commands without resource signal) default to workspaces.cpu.
    if cpu_only is True:
        candidate = config.workspace_cpu_id or config.job_workspace_id
        if candidate:
            _validate_workspace_id(candidate)
        return candidate

    if gpu_type is not None:
        candidate = config.workspace_gpu_id or config.job_workspace_id
        if candidate:
            _validate_workspace_id(candidate)
        return candidate

    candidate = config.workspace_cpu_id or config.job_workspace_id
    if candidate:
        _validate_workspace_id(candidate)
    return candidate
