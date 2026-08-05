"""`inspire serving metrics <name>` — resource-utilization time series for inference servings.

Multi-replica deployments render one line per replica pod. Useful for
catching under-utilized replicas or noisy-neighbor situations on shared
nodes.
"""

from __future__ import annotations

from typing import Optional

from inspire.cli.context import Context
from inspire.cli.utils.metrics_shared import ResolvedMetricsTarget, build_metrics_command
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import WebSession


def _serving_lcg_from_detail(detail: object) -> Optional[str]:
    if not isinstance(detail, dict):
        return None
    lcg = detail.get("logic_compute_group_id")
    if isinstance(lcg, str) and lcg.strip():
        return lcg.strip()
    return None


def _resolve_serving_lcg(task_id: str, session: WebSession) -> Optional[str]:
    detail = browser_api_module.get_serving_detail(
        inference_serving_id=task_id,
        session=session,
    )
    return _serving_lcg_from_detail(detail)


def _serving_name_to_id(
    ctx: Context,
    name: str,
    pick: int | None = None,
) -> ResolvedMetricsTarget:
    from inspire.cli.commands.serving import serving_commands as _sv
    from inspire.platform.web.session import get_web_session

    session = get_web_session()
    workspace_id = _sv._resolve_workspace_id(
        str(getattr(ctx, "workspace", "") or ""),
        session=session,
    )
    serving_id, detail = _sv.run_with_stale_handle_retry(
        name=name,
        resolve_cached=lambda: _sv._resolve_serving_name(
            ctx,
            name,
            workspace_id=workspace_id,
            pick=pick,
        ),
        resolve_live=lambda live_name: _sv._resolve_serving_name(
            ctx,
            live_name,
            workspace_id=workspace_id,
            pick=pick,
            require_live=True,
        ),
        operation=lambda resolved_id: (
            resolved_id,
            browser_api_module.get_serving_detail(
                inference_serving_id=resolved_id,
                session=session,
            ),
        ),
        invalidate=lambda serving_id: _invalidate_serving_handle(
            serving_id,
            session=session,
            name=name,
            workspace_id=workspace_id,
        ),
    )
    return ResolvedMetricsTarget(
        task_id=serving_id,
        logic_compute_group_id=_serving_lcg_from_detail(detail),
    )


def _invalidate_serving_handle(
    serving_id: str,
    *,
    session: WebSession,
    name: str,
    workspace_id: Optional[str],
) -> None:
    from inspire.cli.commands.serving import serving_commands as _sv

    _sv.forget_resource_identity(
        session=session,
        resource_type="serving",
        resource_id=serving_id,
        name=name,
        workspace_id=str(workspace_id or ""),
        owner_scope="self",
    )


serving_metrics = build_metrics_command(
    resource_name="serving",
    resource_label="Serving",
    name_resolver=_serving_name_to_id,
    lcg_resolver=_resolve_serving_lcg,
)


__all__ = ["serving_metrics"]
