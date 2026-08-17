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
        scope="global",
    ),
]

PROXY_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_REQUESTS_HTTP_PROXY",
        toml_key="proxy.requests_http",
        field_name="requests_http_proxy",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_REQUESTS_HTTPS_PROXY",
        toml_key="proxy.requests_https",
        field_name="requests_https_proxy",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_PLAYWRIGHT_PROXY",
        toml_key="proxy.playwright",
        field_name="playwright_proxy",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_RTUNNEL_PROXY",
        toml_key="proxy.rtunnel",
        field_name="rtunnel_proxy",
        scope="global",
    ),
]

AUTH_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_USERNAME",
        toml_key="auth.username",
        field_name="username",
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_PASSWORD",
        toml_key="auth.password",
        field_name="password",
        secret=True,
        scope="global",
    ),
]
