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

from .load_common import _apply_defaults_overrides


def _apply_project_context_and_defaults(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
    project_context: dict[str, Any],
    project_defaults: dict[str, Any],
) -> None:
    _ = project_context

    _apply_defaults_overrides(
        defaults=project_defaults,
        config_dict=config_dict,
        sources=sources,
        source_name=SOURCE_PROJECT,
    )


__all__ = ["_apply_project_context_and_defaults"]
