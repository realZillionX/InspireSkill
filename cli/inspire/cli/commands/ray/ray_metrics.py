"""`inspire ray metrics <name>` — Ray resource-utilization history."""

from __future__ import annotations

from typing import Optional

from inspire.cli.context import Context
from inspire.cli.utils.metrics_shared import ResolvedMetricsTarget, build_metrics_command
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.session import WebSession


def _ray_lcg_from_detail(detail: object) -> Optional[str]:
    """Find a compute-group handle in the Ray detail payload."""
    if isinstance(detail, dict):
        for key in ("logic_compute_group_id", "compute_group_id"):
            value = detail.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in detail.values():
            resolved = _ray_lcg_from_detail(value)
            if resolved:
                return resolved
    elif isinstance(detail, list):
        for value in detail:
            resolved = _ray_lcg_from_detail(value)
            if resolved:
                return resolved
    return None


def _resolve_ray_lcg(task_id: str, session: WebSession) -> Optional[str]:
    return _ray_lcg_from_detail(
        browser_api_module.get_ray_job_detail(task_id, session=session)
    )


def _ray_name_to_id(
    ctx: Context,
    name: str,
    pick: int | None = None,
) -> ResolvedMetricsTarget:
    from inspire.cli.commands.ray import ray_commands as _ray
    from inspire.platform.web.session import get_web_session

    session = get_web_session()
    resolved_id, detail = _ray._run_readonly_ray_operation(
        ctx,
        session=session,
        name=name,
        workspace=str(getattr(ctx, "workspace", "") or ""),
        limit=10000,
        pick=pick,
        operation=lambda task_id, live_session: (
            task_id,
            browser_api_module.get_ray_job_detail(
                task_id,
                session=live_session,
            ),
        ),
    )
    return ResolvedMetricsTarget(
        task_id=resolved_id,
        logic_compute_group_id=_ray_lcg_from_detail(detail),
    )


ray_metrics = build_metrics_command(
    resource_name="ray",
    resource_label="Ray",
    name_resolver=_ray_name_to_id,
    lcg_resolver=_resolve_ray_lcg,
)


__all__ = ["ray_metrics"]
