"""Config options: API and Authentication."""

from __future__ import annotations

from inspire.config.schema_models import (
    ConfigOption,
)

API_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_BASE_URL",
        toml_key="api.base_url",
        field_name="base_url",
        description="API base URL",
        default="https://api.example.com",
        category="API",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_BROWSER_API_PREFIX",
        toml_key="api.browser_api_prefix",
        field_name="browser_api_prefix",
        description="Browser API endpoint path prefix",
        default=None,
        category="API",
        scope="global",
    ),
]

PROXY_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_REQUESTS_HTTP_PROXY",
        toml_key="proxy.requests_http",
        field_name="requests_http_proxy",
        description="HTTP proxy URL for requests/curl style traffic",
        default=None,
        category="Proxy",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_REQUESTS_HTTPS_PROXY",
        toml_key="proxy.requests_https",
        field_name="requests_https_proxy",
        description="HTTPS proxy URL for requests/curl style traffic",
        default=None,
        category="Proxy",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_PLAYWRIGHT_PROXY",
        toml_key="proxy.playwright",
        field_name="playwright_proxy",
        description="Proxy URL for Playwright browser automation",
        default=None,
        category="Proxy",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_RTUNNEL_PROXY",
        toml_key="proxy.rtunnel",
        field_name="rtunnel_proxy",
        description="Proxy URL for rtunnel/SSH ProxyCommand transport",
        default=None,
        category="Proxy",
        scope="global",
    ),
]

AUTH_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_USERNAME",
        toml_key="auth.username",
        field_name="username",
        description="Platform username",
        default=None,
        category="Authentication",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_PASSWORD",
        toml_key="auth.password",
        field_name="password",
        description="Platform password (use env var for security)",
        default=None,
        category="Authentication",
        secret=True,
        scope="global",
    ),
]
