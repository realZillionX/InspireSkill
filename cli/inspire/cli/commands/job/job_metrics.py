"""`inspire job metrics <job-id>` — resource-utilization time series for train_job.

Primary use case: monitoring multi-node distributed training. Every worker
pod renders as its own line in the PNG chart and gets per-pod stats in the
text summary so stragglers (`worker-3` stuck at 0% while the others are at
95%) are immediately visible.

Resolver: POSTs ``/api/v1/train_job/detail`` (Browser API, SSO session)
and reads the top-level ``logic_compute_group_id`` field.
"""

from __future__ import annotations

from typing import Optional

from inspire.cli.utils.metrics_shared import build_metrics_command
from inspire.platform.web.browser_api.core import _browser_api_path, _get_base_url, _request_json
from inspire.platform.web.session import WebSession


def _resolve_job_lcg(task_id: str, session: WebSession) -> Optional[str]:
    data = _request_json(
        session,
        "POST",
        _browser_api_path("/train_job/detail"),
        referer=f"{_get_base_url()}/jobs/distributedTrainingDetail/{task_id}",
        body={"job_id": task_id},
        timeout=30,
    )
    if data.get("code") != 0:
        raise ValueError(f"train_job/detail failed: {data.get('message')}")
    payload = data.get("data")
    if not isinstance(payload, dict):
        return None
    lcg = payload.get("logic_compute_group_id")
    if isinstance(lcg, str) and lcg.strip():
        return lcg.strip()
    return None


job_metrics = build_metrics_command(
    resource_name="job",
    resource_label="Train Job",
    id_arg="job_id",
    id_help="Training job ID (e.g. job-abc-1234...). Keep the 'job-' prefix — the metric backend matches the full ID.",
    lcg_resolver=_resolve_job_lcg,
)


__all__ = ["job_metrics"]
