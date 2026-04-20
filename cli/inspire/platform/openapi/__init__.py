"""Inspire OpenAPI client.

This package contains the token-based OpenAPI client and its domain helpers.
"""

from __future__ import annotations

from inspire.platform.openapi.client import DEFAULT_SHM_ENV_VAR, InspireAPI
from inspire.platform.openapi.endpoints import APIEndpoints
from inspire.platform.openapi.errors import (
    API_ERROR_CODES,
    AuthenticationError,
    InspireAPIError,
    JobCreationError,
    JobNotFoundError,
    ValidationError,
    _translate_api_error,
    _validate_job_id_format,
)
from inspire.platform.openapi.models import ComputeGroup, GPUType, InspireConfig, ResourceSpec
from inspire.platform.openapi.resources import ResourceManager

__all__ = [
    "APIEndpoints",
    "API_ERROR_CODES",
    "AuthenticationError",
    "ComputeGroup",
    "DEFAULT_SHM_ENV_VAR",
    "GPUType",
    "InspireAPI",
    "InspireAPIError",
    "InspireConfig",
    "JobCreationError",
    "JobNotFoundError",
    "ResourceManager",
    "ResourceSpec",
    "ValidationError",
    "_translate_api_error",
    "_validate_job_id_format",
]
