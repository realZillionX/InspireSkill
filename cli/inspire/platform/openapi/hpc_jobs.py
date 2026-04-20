"""HPC job helpers for the Inspire OpenAPI client."""

from __future__ import annotations

from typing import Any, Dict

from inspire.platform.openapi.errors import InspireAPIError, _translate_api_error


def _is_unknown_field_error(message: str, field_name: str) -> bool:
    """Return True when backend reports an unknown protobuf field."""
    lower = (message or "").lower()
    return "unknown field" in lower and f'"{field_name.lower()}"' in lower


def _is_string_field_type_error(message: str, field_name: str) -> bool:
    """Return True when backend reports a string-typed field mismatch."""
    lower = (message or "").lower()
    compact = lower.replace("_", "").replace(" ", "")
    return "invalidvalueforstringfield" in compact and field_name.lower() in compact


def _is_invalid_hpc_spec_error(message: str) -> bool:
    """Return True when backend rejects a non-HPC quota as spec_id."""
    lower = (message or "").lower()
    return "spec_id" in lower and "predef_node_specs" in lower and "not found" in lower


def create_hpc_job(
    api,  # noqa: ANN001
    *,
    name: str,
    logic_compute_group_id: str,
    project_id: str,
    workspace_id: str,
    image: str,
    image_type: str,
    entrypoint: str,
    spec_id: str,
    instance_count: int = 1,
    task_priority: int = 10,
    number_of_tasks: int = 1,
    cpus_per_task: int | str = 1,
    memory_per_cpu: int | str = "4G",
    enable_hyper_threading: bool = False,
) -> Dict[str, Any]:
    """Create an HPC job via /openapi/v1/hpc_jobs/create."""
    api._check_authentication()
    api._validate_required_params(
        name=name,
        logic_compute_group_id=logic_compute_group_id,
        project_id=project_id,
        workspace_id=workspace_id,
        image=image,
        image_type=image_type,
        entrypoint=entrypoint,
        spec_id=spec_id,
    )

    payload = {
        "name": name,
        "logic_compute_group_id": logic_compute_group_id,
        "project_id": project_id,
        "workspace_id": workspace_id,
        "image": image,
        "image_type": image_type,
        "entrypoint": entrypoint,
        "spec_id": spec_id,
        "instance_count": instance_count,
        "task_priority": task_priority,
        "number_of_tasks": number_of_tasks,
        "cpus_per_task": str(cpus_per_task),
        "memory_per_cpu": (
            str(memory_per_cpu) if str(memory_per_cpu)[-1:].isalpha() else f"{memory_per_cpu}G"
        ),
        "enable_hyper_threading": enable_hyper_threading,
    }

    result = api._make_request("POST", api.endpoints.HPC_JOB_CREATE, payload)
    if result.get("code") == 0:
        return result

    # Backend schema changed on some clusters: `task_priority` may be rejected.
    # Retry with `priority`, then without any priority field as final fallback.
    error_msg = str(result.get("message", ""))
    if _is_unknown_field_error(error_msg, "task_priority"):
        payload_retry = dict(payload)
        task_priority_value = payload_retry.pop("task_priority", None)
        if task_priority_value is not None:
            payload_retry["priority"] = task_priority_value

        retry = api._make_request("POST", api.endpoints.HPC_JOB_CREATE, payload_retry)
        if retry.get("code") == 0:
            return retry

        retry_msg = str(retry.get("message", ""))
        if _is_unknown_field_error(retry_msg, "priority"):
            payload_final = dict(payload_retry)
            payload_final.pop("priority", None)
            retry_final = api._make_request("POST", api.endpoints.HPC_JOB_CREATE, payload_final)
            if retry_final.get("code") == 0:
                return retry_final
            result = retry_final
        else:
            result = retry

    # Backend schema changed on some clusters: cpusPerTask/memoryPerCpu are
    # string-typed in proto. Retry with string values.
    error_msg = str(result.get("message", ""))
    if _is_string_field_type_error(error_msg, "memoryPerCpu") or _is_string_field_type_error(
        error_msg, "cpusPerTask"
    ):
        payload_retry_types = dict(payload)
        payload_retry_types["cpus_per_task"] = str(payload_retry_types["cpus_per_task"])
        payload_retry_types["memory_per_cpu"] = str(payload_retry_types["memory_per_cpu"])

        retry_types = api._make_request("POST", api.endpoints.HPC_JOB_CREATE, payload_retry_types)
        if retry_types.get("code") == 0:
            return retry_types

        retry_types_msg = str(retry_types.get("message", ""))
        if _is_unknown_field_error(retry_types_msg, "task_priority"):
            payload_retry_types2 = dict(payload_retry_types)
            task_priority_value = payload_retry_types2.pop("task_priority", None)
            if task_priority_value is not None:
                payload_retry_types2["priority"] = task_priority_value

            retry_types2 = api._make_request(
                "POST", api.endpoints.HPC_JOB_CREATE, payload_retry_types2
            )
            if retry_types2.get("code") == 0:
                return retry_types2

            retry_types2_msg = str(retry_types2.get("message", ""))
            if _is_unknown_field_error(retry_types2_msg, "priority"):
                payload_retry_types3 = dict(payload_retry_types2)
                payload_retry_types3.pop("priority", None)
                retry_types3 = api._make_request(
                    "POST", api.endpoints.HPC_JOB_CREATE, payload_retry_types3
                )
                if retry_types3.get("code") == 0:
                    return retry_types3
                result = retry_types3
            else:
                result = retry_types2
        else:
            result = retry_types

    error_code = result.get("code")
    error_msg = result.get("message", "Unknown error")
    friendly_msg = _translate_api_error(error_code, error_msg)
    if _is_invalid_hpc_spec_error(error_msg):
        friendly_msg = (
            f"{friendly_msg}. Hint: HPC requires the predef_quota_id, not the notebook quota_id. "
            "Run `inspire resources specs --usage hpc --workspace ... --group ...` or inspect "
            "`inspire --json hpc status <job_id>` -> `slurm_cluster_spec.predef_quota_id`."
        )
    raise InspireAPIError(f"Failed to create HPC job: {friendly_msg}")


def get_hpc_job_detail(api, job_id: str) -> Dict[str, Any]:  # noqa: ANN001
    """Get HPC job details via /openapi/v1/hpc_jobs/detail."""
    api._check_authentication()
    api._validate_required_params(job_id=job_id)

    payload = {"job_id": job_id}
    result = api._make_request("POST", api.endpoints.HPC_JOB_DETAIL, payload)
    if result.get("code") == 0:
        return result

    error_code = result.get("code")
    error_msg = result.get("message", "Unknown error")
    friendly_msg = _translate_api_error(error_code, error_msg)
    raise InspireAPIError(f"Failed to get HPC job details: {friendly_msg}")


def stop_hpc_job(api, job_id: str) -> bool:  # noqa: ANN001
    """Stop HPC job via /openapi/v1/hpc_jobs/stop."""
    api._check_authentication()
    api._validate_required_params(job_id=job_id)

    payload = {"job_id": job_id}
    result = api._make_request("POST", api.endpoints.HPC_JOB_STOP, payload)
    if result.get("code") == 0:
        return True

    error_code = result.get("code")
    error_msg = result.get("message", "Unknown error")
    friendly_msg = _translate_api_error(error_code, error_msg)
    raise InspireAPIError(f"Failed to stop HPC job: {friendly_msg}")


__all__ = ["create_hpc_job", "get_hpc_job_detail", "stop_hpc_job"]
