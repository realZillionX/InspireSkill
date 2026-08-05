"""Apply project context and defaults from the current project file."""

from __future__ import annotations

from typing import Any

from inspire.config.models import SOURCE_PROJECT

from .load_common import _apply_defaults_overrides


def _apply_project_context_and_defaults(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
    project_context: dict[str, Any],
    project_defaults: dict[str, Any],
) -> None:
    project_name = str(project_context.get("project") or "").strip()
    if project_name:
        config_dict["context_project"] = project_name
        sources["context_project"] = SOURCE_PROJECT
    workspace_name = str(project_context.get("workspace") or "").strip()
    if workspace_name:
        config_dict["context_workspace"] = workspace_name
        sources["context_workspace"] = SOURCE_PROJECT

    _apply_defaults_overrides(
        defaults=project_defaults,
        config_dict=config_dict,
        sources=sources,
        source_name=SOURCE_PROJECT,
    )


__all__ = ["_apply_project_context_and_defaults"]
