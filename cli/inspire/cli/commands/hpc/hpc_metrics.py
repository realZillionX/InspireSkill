"""`inspire hpc metrics <name>` — resource-utilization time series for HPC jobs.

Primary use case: monitoring multi-task Slurm HPC runs. Each task/pod is
drawn as its own line; divergence exposes bad node placements, hung tasks,
etc.

Resolver: ``GET /api/v1/hpc_jobs/{id}`` (Browser API REST-style path,
confirmed 2026-04) returns the HPC detail blob with a top-level
``logic_compute_group_id`` mirroring the train_job shape.
"""

from __future__ import annotations

from typing import Optional

from inspire.cli.context import Context
from inspire.cli.utils.metrics_shared import build_metrics_command
from inspire.platform.web.browser_api.core import _browser_api_path, _get_base_url, _request_json
from inspire.platform.web.session import WebSession


def _resolve_hpc_lcg(task_id: str, session: WebSession) -> Optional[str]:
    data = _request_json(
        session,
        "GET",
        _browser_api_path(f"/hpc_jobs/{task_id}"),
        referer=f"{_get_base_url()}/jobs/hpcDetail/{task_id}",
        timeout=30,
    )
    if data.get("code") != 0:
        raise ValueError(f"hpc_jobs detail failed: {data.get('message')}")
    payload = data.get("data")
    if not isinstance(payload, dict):
        return None
    lcg = payload.get("logic_compute_group_id")
    if isinstance(lcg, str) and lcg.strip():
        return lcg.strip()
    return None


def _hpc_name_to_id(ctx: Context, name: str) -> str:
    # Module-attribute lookup so pytest monkeypatches on `_resolve_hpc_name`
    # in ``hpc_commands`` intercept at call time (see cli/tests/conftest.py).
    from inspire.cli.commands.hpc import hpc_commands as _hpc

    return _hpc._resolve_hpc_name(ctx, name)


hpc_metrics = build_metrics_command(
    resource_name="hpc",
    resource_label="HPC Job",
    name_resolver=_hpc_name_to_id,
    lcg_resolver=_resolve_hpc_lcg,
)


__all__ = ["hpc_metrics"]
