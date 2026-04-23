"""Project ``[context]`` + ``[defaults]`` application.

Named ``load_accounts`` for history — older revisions merged a legacy
``[accounts."<user>"]`` catalog here, but that entire mechanism is gone.
The only piece still needed is ``_apply_project_context_and_defaults``,
which resolves project-level bindings from the project TOML's
``[context]`` / ``[defaults]`` sections.
"""

from __future__ import annotations

from typing import Any

from inspire.config.models import SOURCE_PROJECT

from .load_common import (
    _CONTEXT_WORKSPACE_FIELD_MAP,
    _apply_defaults_overrides,
    _resolve_alias,
)


def _apply_project_context_and_defaults(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
    project_context: dict[str, Any],
    project_defaults: dict[str, Any],
) -> None:
    project_ref = _resolve_alias(
        project_context.get("project"),
        config_dict.get("projects", {}),
        id_prefix="project-",
    )
    if project_ref:
        config_dict["job_project_id"] = project_ref
        sources["job_project_id"] = SOURCE_PROJECT

    for context_key, field_name in _CONTEXT_WORKSPACE_FIELD_MAP.items():
        workspace_ref = _resolve_alias(
            project_context.get(context_key),
            config_dict.get("workspaces", {}),
            id_prefix="ws-",
        )
        if not workspace_ref:
            continue
        config_dict[field_name] = workspace_ref
        sources[field_name] = SOURCE_PROJECT

    _apply_defaults_overrides(
        defaults=project_defaults,
        config_dict=config_dict,
        sources=sources,
        source_name=SOURCE_PROJECT,
    )


__all__ = ["_apply_project_context_and_defaults"]
