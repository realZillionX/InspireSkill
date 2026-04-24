"""`inspire serving metrics <name>` — resource-utilization time series for inference servings.

Multi-replica deployments render one line per replica pod. Useful for
catching under-utilized replicas or noisy-neighbor situations on shared
nodes.

Resolver: Browser-API ``GET /api/v1/inference_servings/detail`` returns a
top-level ``logic_compute_group_id`` (already surfaced by
:class:`browser_api.servings.ServingInfo`).
"""

from __future__ import annotations

from typing import Optional

from inspire.cli.context import Context
from inspire.cli.utils.metrics_shared import build_metrics_command
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import WebSession


def _resolve_serving_lcg(task_id: str, session: WebSession) -> Optional[str]:
    detail = browser_api_module.get_serving_detail(
        inference_serving_id=task_id, session=session
    )
    if not isinstance(detail, dict):
        return None
    lcg = detail.get("logic_compute_group_id")
    if isinstance(lcg, str) and lcg.strip():
        return lcg.strip()
    return None


def _serving_name_to_id(ctx: Context, name: str) -> str:
    from inspire.cli.commands.serving import serving_commands as _sv

    return _sv._resolve_serving_name(ctx, name)


serving_metrics = build_metrics_command(
    resource_name="serving",
    resource_label="Serving",
    name_resolver=_serving_name_to_id,
    lcg_resolver=_resolve_serving_lcg,
)


__all__ = ["serving_metrics"]
