"""Config options for notebook tunnel retries."""

from __future__ import annotations

from inspire.config.schema_models import (
    ConfigOption,
    _parse_float,
    _parse_int,
)

TUNNEL_OPTIONS: list[ConfigOption] = [
    ConfigOption(
        env_var="INSPIRE_TUNNEL_RETRIES",
        toml_key="tunnel.retries",
        field_name="tunnel_retries",
        parser=_parse_int,
        scope="global",
    ),
    ConfigOption(
        env_var="INSPIRE_TUNNEL_RETRY_PAUSE",
        toml_key="tunnel.retry_pause",
        field_name="tunnel_retry_pause",
        parser=_parse_float,
        scope="global",
    ),
]
