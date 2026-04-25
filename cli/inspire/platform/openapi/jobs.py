"""Job-related helpers for the Inspire OpenAPI client."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests

from inspire.platform.openapi.errors import (
    JobCreationError,
    JobNotFoundError,
    InspireAPIError,
    ValidationError,
    _translate_api_error,
    _validate_job_id_format,
)

logger = logging.getLogger(__name__)


def create_training_job_smart(
    api,  # noqa: ANN001
    *,
    name: str,
    command: str,
    framework: str = "pytorch",
    project_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    image: Optional[str] = None,
    task_priority: Optional[int] = None,
    instance_count: Optional[int] = None,
    max_running_time_ms: Optional[str] = None,
    shm_gi: Optional[int] = None,
    spec_id_override: Optional[str] = None,
    compute_group_id_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a training job from a pre-resolved quota.

    Callers must resolve ``spec_id_override`` (the quota ID) and
    ``compute_group_id_override`` via
    :func:`inspire.cli.utils.quota_resolver.resolve_quota` before calling.
    """
    api._check_authentication()
    api._validate_required_params(name=name, command=command)

    if not (spec_id_override and compute_group_id_override):
        raise ValidationError(
            "create_training_job_smart requires spec_id_override + "
            "compute_group_id_override (resolve via "
            "inspire.cli.utils.quota_resolver.resolve_quota)."
        )
    spec_id = spec_id_override
    compute_group_id = compute_group_id_override

    project_id = project_id or api.DEFAULT_PROJECT_ID
    workspace_id = workspace_id or api.DEFAULT_WORKSPACE_ID
    task_priority = task_priority or api.DEFAULT_TASK_PRIORITY
    instance_count = instance_count or api.DEFAULT_INSTANCE_COUNT
    max_running_time_ms = max_running_time_ms or api.DEFAULT_MAX_RUNNING_TIME

    if shm_gi is None:
        shm_gi = api.DEFAULT_SHM_SIZE

    final_image = image or api._get_default_image()

    framework_item = {
        "image_type": api.DEFAULT_IMAGE_TYPE,
        "image": final_image,
        "instance_count": instance_count,
        "spec_id": spec_id,
    }
    if shm_gi is not None:
        framework_item["shm_gi"] = shm_gi

    payload = {
        "name": name,
        "command": command,
        "framework": framework,
        "logic_compute_group_id": compute_group_id,
        "project_id": project_id,
        "workspace_id": workspace_id,
        "task_priority": task_priority,
        "max_running_time_ms": max_running_time_ms,
        "framework_config": [framework_item],
    }

    try:
        result = api._make_request("POST", api.endpoints.TRAIN_JOB_CREATE, payload)

        if result.get("code") == 0:
            job_id = result["data"].get("job_id")
            logger.info("🚀 Training job created successfully! Job ID: %s", job_id)
            return result

        error_code = result.get("code")
        error_msg = result.get("message", "Unknown error")
        friendly_msg = _translate_api_error(error_code, error_msg)
        raise JobCreationError(f"Failed to create training job: {friendly_msg}")

    except requests.exceptions.RequestException as e:
        raise JobCreationError(f"Training job creation request failed: {str(e)}") from e


def get_job_detail(api, job_id: str) -> Dict[str, Any]:  # noqa: ANN001
    """Get training job details."""
    api._check_authentication()
    api._validate_required_params(job_id=job_id)

    format_error = _validate_job_id_format(job_id)
    if format_error:
        raise JobNotFoundError(f"Invalid job ID '{job_id}': {format_error}")

    payload = {"job_id": job_id}
    result = api._make_request("POST", api.endpoints.TRAIN_JOB_DETAIL, payload)

    if result.get("code") == 0:
        logger.info("📋 Retrieved details for job %s", job_id)
        return result

    error_code = result.get("code")
    error_msg = result.get("message", "Unknown error")
    friendly_msg = _translate_api_error(error_code, error_msg)
    if error_code == 100002:
        raise JobNotFoundError(f"Failed to get job details for '{job_id}': {friendly_msg}")
    raise InspireAPIError(f"Failed to get job details: {friendly_msg}")


def stop_training_job(api, job_id: str) -> bool:  # noqa: ANN001
    """Stop training job."""
    api._check_authentication()
    api._validate_required_params(job_id=job_id)

    format_error = _validate_job_id_format(job_id)
    if format_error:
        raise JobNotFoundError(f"Invalid job ID '{job_id}': {format_error}")

    payload = {"job_id": job_id}
    result = api._make_request("POST", api.endpoints.TRAIN_JOB_STOP, payload)

    if result.get("code") == 0:
        logger.info("🛑 Training job %s stopped successfully.", job_id)
        return True

    error_code = result.get("code")
    error_msg = result.get("message", "Unknown error")
    friendly_msg = _translate_api_error(error_code, error_msg)
    if error_code == 100002:
        raise JobNotFoundError(f"Failed to stop job '{job_id}': {friendly_msg}")
    raise InspireAPIError(f"Failed to stop training job: {friendly_msg}")


__all__ = ["create_training_job_smart", "get_job_detail", "stop_training_job"]
