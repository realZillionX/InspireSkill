"""OpenAPI domain models for the Inspire API client."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class GPUType(Enum):
    """GPU type enumeration."""

    H100 = "H100"
    H200 = "H200"


@dataclass
class ResourceSpec:
    """Resource specification configuration."""

    gpu_type: GPUType
    gpu_count: int
    cpu_cores: int
    memory_gb: int
    gpu_memory_gb: int
    spec_id: str
    description: str


@dataclass
class ComputeGroup:
    """Compute group configuration."""

    name: str
    compute_group_id: str
    gpu_type: GPUType
    location: str = ""


@dataclass
class InspireConfig:
    """Inspire API configuration class."""

    base_url: str = "https://api.example.com"
    timeout: int = 30
    max_retries: int = 3
    retry_delay: float = 1.0
    verify_ssl: bool = True  # Can be disabled via INSPIRE_SKIP_SSL_VERIFY env var
    force_proxy: bool = False  # Can be enabled via INSPIRE_FORCE_PROXY env var
    # API path prefixes (None = use code defaults)
    openapi_prefix: Optional[str] = None
    auth_endpoint: Optional[str] = None
    docker_registry: Optional[str] = None  # Docker registry hostname
    # Compute groups configuration
    compute_groups: Optional[list[dict]] = None  # List of compute group dicts from config
    # Proxy configuration
    force_proxy: bool = False
    requests_http_proxy: Optional[str] = None
    requests_https_proxy: Optional[str] = None
