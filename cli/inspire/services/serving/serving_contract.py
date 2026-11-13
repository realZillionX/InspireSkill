"""Pure validation of the console's custom-serving creation fields."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


_PORT_PATHS = {"PORT_TYPE_HTTP": "/", "PORT_TYPE_METRICS": "/metrics"}


def normalize_port_configs(
    port: int | None = None,
    port_configs: Sequence[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Translate legacy ``port`` without sending the obsolete scalar field.

    The console has one required HTTP endpoint and an optional metrics endpoint.
    Both can use the same container port (for example an HTTP /metrics route).
    Supplying both interfaces is allowed only when their HTTP ports agree.
    """
    if port is not None and (type(port) is not int or not 1 <= port <= 65535):
        raise ValueError("port must be an integer between 1 and 65535.")
    if port_configs is None:
        if port is None:
            raise ValueError("Provide port or an HTTP entry in port_configs.")
        return [{"port": port, "type": "PORT_TYPE_HTTP", "path": "/"}]
    if not isinstance(port_configs, (list, tuple)) or not port_configs:
        raise ValueError("port_configs must be a non-empty list or tuple.")
    entries: dict[str, dict[str, Any]] = {}
    for entry in port_configs:
        if not isinstance(entry, Mapping) or set(entry) != {"port", "type", "path"}:
            raise ValueError("Each port_configs entry must contain port, type and path only.")
        kind = entry["type"]
        if not isinstance(kind, str) or kind not in _PORT_PATHS:
            raise ValueError("Port type must be PORT_TYPE_HTTP or PORT_TYPE_METRICS.")
        if kind in entries:
            raise ValueError("Only one entry per port type is supported.")
        number = entry["port"]
        if type(number) is not int or not 1 <= number <= 65535:
            raise ValueError("Each port must be an integer between 1 and 65535.")
        if entry["path"] != _PORT_PATHS[kind]:
            raise ValueError("HTTP path must be '/' and metrics path must be '/metrics'.")
        entries[kind] = {"port": number, "type": kind, "path": entry["path"]}
    if "PORT_TYPE_HTTP" not in entries:
        raise ValueError("port_configs requires one PORT_TYPE_HTTP entry.")
    if port is not None and port != entries["PORT_TYPE_HTTP"]["port"]:
        raise ValueError("port conflicts with the HTTP port in port_configs.")
    return [entries[kind] for kind in _PORT_PATHS if kind in entries]


def model_create_fields(model_source: str | None, model_version: int | str | None) -> dict:
    """Keep plaza create semantics separate from registry and update versions."""
    if model_source not in (None, "MODEL_PLAZA"):
        raise ValueError("model_source must be MODEL_PLAZA or omitted for a registered model.")
    if model_source == "MODEL_PLAZA":
        if model_version is not None:
            raise ValueError("Omit model_version when creating from MODEL_PLAZA.")
        return {"model_source": "MODEL_PLAZA"}
    if isinstance(model_version, str) and model_version.isdecimal():
        model_version = int(model_version)
    if type(model_version) is not int or model_version < 1:
        raise ValueError("A registered model requires a positive model_version.")
    return {"model_version": model_version}
