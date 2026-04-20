"""Endpoint definitions for Inspire OpenAPI client."""

from typing import Optional


class APIEndpoints:
    """API endpoint paths with configurable prefixes.

    Uses configured prefixes if provided, otherwise falls back to
    hardcoded defaults for backward compatibility.
    """

    DEFAULT_AUTH_ENDPOINT = "/auth/token"
    DEFAULT_OPENAPI_PREFIX = "/openapi/v1"

    def __init__(self, auth_endpoint: Optional[str] = None, openapi_prefix: Optional[str] = None):
        self._auth_endpoint = auth_endpoint or self.DEFAULT_AUTH_ENDPOINT
        self._openapi_prefix = openapi_prefix or self.DEFAULT_OPENAPI_PREFIX

    @property
    def AUTH_TOKEN(self) -> str:
        return self._auth_endpoint

    @property
    def TRAIN_JOB_CREATE(self) -> str:
        return f"{self._openapi_prefix}/train_job/create"

    @property
    def TRAIN_JOB_DETAIL(self) -> str:
        return f"{self._openapi_prefix}/train_job/detail"

    @property
    def TRAIN_JOB_STOP(self) -> str:
        return f"{self._openapi_prefix}/train_job/stop"

    @property
    def HPC_JOB_CREATE(self) -> str:
        return f"{self._openapi_prefix}/hpc_jobs/create"

    @property
    def HPC_JOB_DETAIL(self) -> str:
        return f"{self._openapi_prefix}/hpc_jobs/detail"

    @property
    def HPC_JOB_STOP(self) -> str:
        return f"{self._openapi_prefix}/hpc_jobs/stop"

    @property
    def CLUSTER_NODES_LIST(self) -> str:
        return f"{self._openapi_prefix}/cluster_nodes/list"

    @property
    def INFERENCE_SERVING_CREATE(self) -> str:
        return f"{self._openapi_prefix}/inference_servings/create"

    @property
    def INFERENCE_SERVING_DETAIL(self) -> str:
        return f"{self._openapi_prefix}/inference_servings/detail"

    @property
    def INFERENCE_SERVING_STOP(self) -> str:
        return f"{self._openapi_prefix}/inference_servings/stop"
