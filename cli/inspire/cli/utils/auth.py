"""Authentication management for Inspire CLI.

Provides authenticated API client with token caching.
"""

import time
from typing import Optional

from inspire.platform.openapi import AuthenticationError, InspireAPI, InspireConfig
from inspire.config import Config


class AuthManager:
    """Manages authentication and provides API client instances.

    Caches tokens for reuse within a session (tokens expire after ~1 hour).
    """

    _token: Optional[str] = None
    _expires_at: float = 0
    _api: Optional[InspireAPI] = None

    @classmethod
    def get_api(cls, config: Optional[Config] = None) -> InspireAPI:
        """Get an authenticated API client.

        Args:
            config: Configuration to use. If None, reads from environment.

        Returns:
            Authenticated InspireAPI instance

        Raises:
            ConfigError: If required environment variables are missing
            AuthenticationError: If authentication fails
        """
        if config is None:
            config = Config.from_env()

        # Check if we have a valid cached token
        if cls._api is not None and cls._token and time.time() < cls._expires_at:
            return cls._api

        # Create new API client
        api_config = InspireConfig(
            base_url=config.base_url,
            timeout=config.timeout,
            max_retries=config.max_retries,
            retry_delay=config.retry_delay,
            verify_ssl=not config.skip_ssl_verify,
            force_proxy=config.force_proxy,
            openapi_prefix=config.openapi_prefix,
            auth_endpoint=config.auth_endpoint,
            docker_registry=config.docker_registry,
            compute_groups=config.compute_groups,
            requests_http_proxy=config.requests_http_proxy,
            requests_https_proxy=config.requests_https_proxy,
        )
        api = InspireAPI(api_config)

        # Authenticate
        try:
            api.authenticate(config.username, config.password)
        except AuthenticationError as e:
            raise AuthenticationError(f"Authentication failed: {e}")
        except Exception as e:
            raise AuthenticationError(f"Authentication request failed: {e}")

        # Cache the token (expire 10 minutes early for safety)
        cls._token = api.token
        cls._expires_at = time.time() + 3000  # ~50 minutes
        cls._api = api

        return api

    @classmethod
    def clear_cache(cls) -> None:
        """Clear cached authentication."""
        cls._token = None
        cls._expires_at = 0
        cls._api = None
