"""OpenAPI domain models for the Inspire API client."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class InspireConfig:
    """Inspire API configuration class."""

    base_url: str = "https://api.example.com"
    timeout: int = 30
    max_retries: int = 3
    retry_delay: float = 1.0
    verify_ssl: bool = True  # Can be disabled via INSPIRE_SKIP_SSL_VERIFY env var
    force_proxy: bool = False  # Can be enabled via INSPIRE_FORCE_PROXY env var
    openapi_prefix: Optional[str] = None
    auth_endpoint: Optional[str] = None
    docker_registry: Optional[str] = None
    compute_groups: Optional[list[dict]] = None
    requests_http_proxy: Optional[str] = None
    requests_https_proxy: Optional[str] = None
