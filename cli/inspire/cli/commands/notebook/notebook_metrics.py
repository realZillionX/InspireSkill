"""`inspire notebook metrics <id>` — thin wrapper around the shared metrics core.

Provides the notebook-specific ``logic_compute_group_id`` resolver (Browser-API
notebook detail → ``start_config.logic_compute_group_id``) and delegates
everything else to :func:`inspire.cli.utils.metrics_shared.build_metrics_command`.
"""

from __future__ import annotations

from typing import Optional

from inspire.cli.utils.metrics_shared import build_metrics_command
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import WebSession


def _resolve_notebook_lcg(task_id: str, session: WebSession) -> Optional[str]:
    """Pull ``logic_compute_group_id`` from the notebook detail payload.

    The live field is ``start_config.logic_compute_group_id`` (verified 2026-04
    via ``GET /api/v1/notebook/{id}``). The top-level ``logic_compute_group.*``
    object exists but platform-side leaves its ID fields empty — keep a
    defensive fallback in case they populate it later.
    """
    detail = browser_api_module.get_notebook_detail(notebook_id=task_id, session=session)
    if not isinstance(detail, dict):
        return None
    start_cfg = detail.get("start_config")
    if isinstance(start_cfg, dict):
        lcg = start_cfg.get("logic_compute_group_id")
        if isinstance(lcg, str) and lcg.strip():
            return lcg.strip()
    grp = detail.get("logic_compute_group")
    if isinstance(grp, dict):
        for key in ("logic_compute_group_id", "compute_group_id"):
            value = grp.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


notebook_metrics = build_metrics_command(
    resource_name="notebook",
    resource_label="Notebook",
    id_arg="notebook_id",
    id_help="Notebook ID (e.g. 91fbc44e-9c40-4c99-99f4-d27d6303266e).",
    lcg_resolver=_resolve_notebook_lcg,
)


__all__ = ["notebook_metrics"]
