"""Inference serving (model deployment) helpers for the OpenAPI client.

Official spec: [references/openapi.md](../../../../references/openapi.md) §3.
The 3 endpoints wrapped here — `create`, `detail`, `stop` — are Bearer-token
authenticated. For list / configs / available project+user, see
`platform.web.browser_api.servings` (Browser API only).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from inspire.platform.openapi.errors import InspireAPIError, _translate_api_error


def create_inference_serving(
    api,  # noqa: ANN001
    *,
    name: str,
    logic_compute_group_id: str,
    project_id: str,
    workspace_id: str,
    image: str,
    image_type: str,
    command: str,
    model_id: str,
    model_version: int,
    port: int,
    replicas: int,
    node_num_per_replica: int,
    spec_id: str,
    task_priority: int = 10,
    custom_domain: Optional[str] = None,
) -> Dict[str, Any]:
    """Create an inference serving via `/openapi/v1/inference_servings/create`."""
    api._check_authentication()
    api._validate_required_params(
        name=name,
        logic_compute_group_id=logic_compute_group_id,
        project_id=project_id,
        workspace_id=workspace_id,
        image=image,
        image_type=image_type,
        command=command,
        model_id=model_id,
        spec_id=spec_id,
        model_version=model_version,
        port=port,
        replicas=replicas,
        node_num_per_replica=node_num_per_replica,
    )

    def _coerce_int(value: Any, field: str) -> int:
        # `bool` is an `int` subclass in Python (True→1, False→0); accepting it
        # silently would let `replicas=True` pass. Reject explicitly.
        if isinstance(value, bool):
            raise InspireAPIError(
                f"Invalid value for '{field}': expected int, got bool {value!r}"
            )
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            # Floats that aren't whole numbers (e.g. 1.7) would be silently
            # truncated by int(); surface that rather than sending a wrong
            # integer to the platform.
            if not value.is_integer():
                raise InspireAPIError(
                    f"Invalid value for '{field}': expected integer, got float {value!r}"
                )
            return int(value)
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise InspireAPIError(
                f"Invalid value for '{field}': expected int, got {value!r}"
            ) from exc

    payload: dict[str, Any] = {
        "name": name,
        "logic_compute_group_id": logic_compute_group_id,
        "project_id": project_id,
        "image": image,
        "image_type": image_type,
        "command": command,
        "model_id": model_id,
        "model_version": _coerce_int(model_version, "model_version"),
        "port": _coerce_int(port, "port"),
        "replicas": _coerce_int(replicas, "replicas"),
        "node_num_per_replica": _coerce_int(node_num_per_replica, "node_num_per_replica"),
        "task_priority": _coerce_int(task_priority, "task_priority"),
        "workspace_id": workspace_id,
        "spec_id": spec_id,
    }
    if custom_domain:
        payload["custom_domain"] = custom_domain

    result = api._make_request("POST", api.endpoints.INFERENCE_SERVING_CREATE, payload)
    if result.get("code") == 0:
        return result

    code = result.get("code")
    message = result.get("message", "Unknown error")
    raise InspireAPIError(
        f"Failed to create inference serving: {_translate_api_error(code, message)}"
    )


def get_inference_serving_detail(api, inference_serving_id: str) -> Dict[str, Any]:  # noqa: ANN001
    """Get inference serving detail via `/openapi/v1/inference_servings/detail`."""
    api._check_authentication()
    api._validate_required_params(inference_serving_id=inference_serving_id)

    payload = {"inference_serving_id": inference_serving_id}
    result = api._make_request("POST", api.endpoints.INFERENCE_SERVING_DETAIL, payload)
    if result.get("code") == 0:
        return result

    code = result.get("code")
    message = result.get("message", "Unknown error")
    raise InspireAPIError(
        f"Failed to get inference serving detail: {_translate_api_error(code, message)}"
    )


def stop_inference_serving(api, inference_serving_id: str) -> bool:  # noqa: ANN001
    """Stop inference serving via `/openapi/v1/inference_servings/stop`."""
    api._check_authentication()
    api._validate_required_params(inference_serving_id=inference_serving_id)

    payload = {"inference_serving_id": inference_serving_id}
    result = api._make_request("POST", api.endpoints.INFERENCE_SERVING_STOP, payload)
    if result.get("code") == 0:
        return True

    code = result.get("code")
    message = result.get("message", "Unknown error")
    raise InspireAPIError(
        f"Failed to stop inference serving: {_translate_api_error(code, message)}"
    )


__all__ = [
    "create_inference_serving",
    "get_inference_serving_detail",
    "stop_inference_serving",
]
