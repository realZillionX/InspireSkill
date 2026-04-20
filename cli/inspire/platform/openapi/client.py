"""Inspire OpenAPI client (extracted from legacy script).

Provides functionality to:
- Authenticate with the Inspire API
- Create distributed training jobs with smart resource matching
- Query training job details
- Stop training jobs
- List cluster nodes

New Features:
- Natural language resource specification (e.g., "H200", "H100", "4xH200")
- Automatic spec-id and compute-group-id matching
- Interactive resource selection
- Enhanced user experience

API Documentation: https://api.example.com/openapi/
"""

import logging
import os
from typing import Any, Dict, Optional

import requests
import urllib3

from inspire.platform.openapi.auth import authenticate as _authenticate
from inspire.platform.openapi.auth import check_authentication as _check_authentication
from inspire.platform.openapi.http import make_request as _make_request
from inspire.platform.openapi.http import make_request_with_retry as _make_request_with_retry
from inspire.platform.openapi.hpc_jobs import create_hpc_job as _create_hpc_job
from inspire.platform.openapi.hpc_jobs import get_hpc_job_detail as _get_hpc_job_detail
from inspire.platform.openapi.hpc_jobs import stop_hpc_job as _stop_hpc_job
from inspire.platform.openapi.inference_servings import create_inference_serving as _create_inference_serving
from inspire.platform.openapi.inference_servings import get_inference_serving_detail as _get_inference_serving_detail
from inspire.platform.openapi.inference_servings import stop_inference_serving as _stop_inference_serving
from inspire.platform.openapi.jobs import create_training_job_smart as _create_training_job_smart
from inspire.platform.openapi.jobs import get_job_detail as _get_job_detail
from inspire.platform.openapi.jobs import stop_training_job as _stop_training_job
from inspire.platform.openapi.nodes import list_cluster_nodes as _list_cluster_nodes
from inspire.platform.openapi.endpoints import APIEndpoints
from inspire.platform.openapi.errors import (
    ValidationError,
)
from inspire.platform.openapi.models import InspireConfig
from inspire.platform.openapi.resources import ResourceManager

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

DEFAULT_SHM_ENV_VAR = "INSPIRE_SHM_SIZE"
_TRUE_VALUES = {"1", "true", "yes", "on"}


def _get_default_shm_size(fallback: int = 200) -> int:
    """Read default shared memory size from env, falling back to a sane default."""
    env_value = os.getenv(DEFAULT_SHM_ENV_VAR)
    if env_value:
        try:
            value = int(env_value)
            if value >= 1:
                return value
            logger.warning(
                "Environment variable %s must be >= 1 (got %s). Falling back to %s Gi.",
                DEFAULT_SHM_ENV_VAR,
                env_value,
                fallback,
            )
        except ValueError:
            logger.warning(
                "Environment variable %s must be an integer (got %s). Falling back to %s Gi.",
                DEFAULT_SHM_ENV_VAR,
                env_value,
                fallback,
            )
    return fallback


def _as_bool(value: object) -> bool:
    text = str(value or "").strip().lower()
    return text in _TRUE_VALUES


def _normalize_proxy(value: object) -> str:
    return str(value or "").strip()


def _build_http_https_pair(http_value: object, https_value: object) -> dict[str, str]:
    http_proxy = _normalize_proxy(http_value)
    https_proxy = _normalize_proxy(https_value)
    if not http_proxy and not https_proxy:
        return {}
    return {
        "http": http_proxy or https_proxy,
        "https": https_proxy or http_proxy,
    }


def _resolve_openapi_request_proxies(config: InspireConfig) -> tuple[dict[str, str], str]:
    explicit_http = _normalize_proxy(os.environ.get("INSPIRE_REQUESTS_HTTP_PROXY"))
    explicit_https = _normalize_proxy(os.environ.get("INSPIRE_REQUESTS_HTTPS_PROXY"))
    if explicit_http or explicit_https:
        return _build_http_https_pair(explicit_http, explicit_https), "explicit_env"

    toml_http = _normalize_proxy(getattr(config, "requests_http_proxy", None))
    toml_https = _normalize_proxy(getattr(config, "requests_https_proxy", None))
    if toml_http or toml_https:
        return _build_http_https_pair(toml_http, toml_https), "toml"

    system_http = _normalize_proxy(os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY"))
    system_https = _normalize_proxy(os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY"))
    if system_http or system_https:
        return _build_http_https_pair(system_http, system_https), "system_env"

    return {}, "none"


def _resolve_force_proxy(config: InspireConfig) -> bool:
    explicit_force = os.environ.get("INSPIRE_FORCE_PROXY")
    if explicit_force is not None:
        return _as_bool(explicit_force)
    return bool(getattr(config, "force_proxy", False))


class InspireAPI:
    """
    Inspire API Client - Smart Resource Matching Version
    """

    # Default value constants
    DEFAULT_TASK_PRIORITY = 8
    DEFAULT_INSTANCE_COUNT = 1
    DEFAULT_SHM_SIZE = _get_default_shm_size()
    DEFAULT_MAX_RUNNING_TIME = "360000000"  # 100 hours
    DEFAULT_IMAGE_TYPE = "SOURCE_PRIVATE"
    DEFAULT_PROJECT_ID = os.getenv(
        "INSPIRE_PROJECT_ID",
        "project-00000000-0000-0000-0000-000000000000",  # Placeholder - set INSPIRE_PROJECT_ID env var
    )
    DEFAULT_WORKSPACE_ID = os.getenv(
        "INSPIRE_WORKSPACE_ID",
        "ws-00000000-0000-0000-0000-000000000000",  # Placeholder - set INSPIRE_WORKSPACE_ID env var
    )
    DEFAULT_IMAGE = "docker.example.com/inspire-studio/ngc-cuda12.8-base:1.0"
    DEFAULT_IMAGE_PATH = "inspire-studio/ngc-cuda12.8-base:1.0"
    DEFAULT_DOCKER_REGISTRY = "docker.example.com"
    ERROR_BODY_PREVIEW_LIMIT = 4000

    def _get_default_image(self) -> str:
        """Get the default Docker image, using configurable registry if set."""
        if self.config.docker_registry:
            return f"{self.config.docker_registry}/{self.DEFAULT_IMAGE_PATH}"
        return self.DEFAULT_IMAGE

    def __init__(self, config: Optional[InspireConfig] = None):
        """
        Initialize API client.

        Args:
            config: API configuration object, uses default config if None
        """
        self.config = config or InspireConfig()

        # Check for SSL verification override via environment variable
        if os.getenv("INSPIRE_SKIP_SSL_VERIFY", "").lower() in ("1", "true", "yes"):
            self.config.verify_ssl = False

        self.base_url = self.config.base_url.rstrip("/")
        self.token = None
        self.headers = {"Content-Type": "application/json", "Accept": "application/json"}

        # Initialize API endpoints with configurable prefixes
        self.endpoints = APIEndpoints(
            auth_endpoint=self.config.auth_endpoint,
            openapi_prefix=self.config.openapi_prefix,
        )

        # Initialize resource manager
        self.resource_manager = ResourceManager(self.config.compute_groups)

        # Use simple requests session
        self.session = requests.Session()
        # Keep system env/no_proxy behavior unless force proxy is explicitly enabled.
        self.session.trust_env = True
        proxies, proxy_source = _resolve_openapi_request_proxies(self.config)
        if proxies:
            self.session.proxies = proxies
            logger.debug("OpenAPI request proxies (%s): %s", proxy_source, proxies)

        if _resolve_force_proxy(self.config):
            # Disable trust_env to prevent no_proxy/system proxy bypass when force_proxy is enabled.
            self.session.trust_env = False
            if proxies:
                logger.debug(
                    "OpenAPI force_proxy enabled; enforcing resolved proxies (%s): %s",
                    proxy_source,
                    proxies,
                )
            else:
                logger.warning(
                    "OpenAPI force_proxy enabled, but no proxy is configured "
                    "(checked INSPIRE_REQUESTS_*_PROXY, TOML [proxy], and system *_proxy env)."
                )

    def _validate_required_params(self, **kwargs) -> None:
        """Validate required parameters."""
        for param_name, param_value in kwargs.items():
            if param_value is None or (isinstance(param_value, str) and not param_value.strip()):
                raise ValidationError(f"Required parameter '{param_name}' cannot be empty")

    def _make_request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        return _make_request_with_retry(self, method, url, **kwargs)

    def _make_request(self, method: str, endpoint: str, payload: Optional[Dict] = None) -> Dict:
        return _make_request(self, method, endpoint, payload)

    def authenticate(self, username: str, password: str) -> bool:
        return _authenticate(self, username, password)

    def _check_authentication(self) -> None:
        _check_authentication(self)

    def create_training_job_smart(
        self,
        name: str,
        command: str,
        resource: str,
        framework: str = "pytorch",
        prefer_location: Optional[str] = None,
        project_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        image: Optional[str] = None,
        task_priority: Optional[int] = None,
        instance_count: Optional[int] = None,
        max_running_time_ms: Optional[str] = None,
        shm_gi: Optional[int] = None,
    ) -> Dict[str, Any]:
        return _create_training_job_smart(
            self,
            name=name,
            command=command,
            resource=resource,
            framework=framework,
            prefer_location=prefer_location,
            project_id=project_id,
            workspace_id=workspace_id,
            image=image,
            task_priority=task_priority,
            instance_count=instance_count,
            max_running_time_ms=max_running_time_ms,
            shm_gi=shm_gi,
        )

    def get_job_detail(self, job_id: str) -> Dict[str, Any]:
        return _get_job_detail(self, job_id)

    def stop_training_job(self, job_id: str) -> bool:
        return _stop_training_job(self, job_id)

    def create_hpc_job(
        self,
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
        cpus_per_task: int = 1,
        memory_per_cpu: int = 4,
        enable_hyper_threading: bool = False,
    ) -> Dict[str, Any]:
        return _create_hpc_job(
            self,
            name=name,
            logic_compute_group_id=logic_compute_group_id,
            project_id=project_id,
            workspace_id=workspace_id,
            image=image,
            image_type=image_type,
            entrypoint=entrypoint,
            spec_id=spec_id,
            instance_count=instance_count,
            task_priority=task_priority,
            number_of_tasks=number_of_tasks,
            cpus_per_task=cpus_per_task,
            memory_per_cpu=memory_per_cpu,
            enable_hyper_threading=enable_hyper_threading,
        )

    def get_hpc_job_detail(self, job_id: str) -> Dict[str, Any]:
        return _get_hpc_job_detail(self, job_id)

    def stop_hpc_job(self, job_id: str) -> bool:
        return _stop_hpc_job(self, job_id)

    def create_inference_serving(
        self,
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
        return _create_inference_serving(
            self,
            name=name,
            logic_compute_group_id=logic_compute_group_id,
            project_id=project_id,
            workspace_id=workspace_id,
            image=image,
            image_type=image_type,
            command=command,
            model_id=model_id,
            model_version=model_version,
            port=port,
            replicas=replicas,
            node_num_per_replica=node_num_per_replica,
            spec_id=spec_id,
            task_priority=task_priority,
            custom_domain=custom_domain,
        )

    def get_inference_serving_detail(self, inference_serving_id: str) -> Dict[str, Any]:
        return _get_inference_serving_detail(self, inference_serving_id)

    def stop_inference_serving(self, inference_serving_id: str) -> bool:
        return _stop_inference_serving(self, inference_serving_id)

    def list_cluster_nodes(
        self, page_num: int = 1, page_size: int = 10, resource_pool: Optional[str] = None
    ) -> Dict[str, Any]:
        return _list_cluster_nodes(
            self,
            page_num=page_num,
            page_size=page_size,
            resource_pool=resource_pool,
        )
