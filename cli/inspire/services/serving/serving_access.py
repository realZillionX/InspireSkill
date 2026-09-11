"""Validated serving invocation metadata, with no credentials."""

from __future__ import annotations

import ipaddress
import re
import shlex
from typing import Any
from urllib.parse import urlsplit


def serving_endpoint(item: object) -> str:
    raw = item if isinstance(item, dict) else getattr(item, "raw", None)
    extra = raw.get("extra_info") if isinstance(raw, dict) else None
    value = extra.get("service") if isinstance(extra, dict) else None
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
        return ""
    try:
        parsed = urlsplit(value)
        # Observed platform addresses are origins. Reject unknown path/token
        # formats instead of publishing an unverified credential-bearing URL.
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return ""
        if (
            parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "?" in value
            or "#" in value
        ):
            return ""
        host = parsed.hostname
        if ":" in host:
            ipaddress.IPv6Address(host)
        elif not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            return ""
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return ""
    except ValueError:
        return ""
    return value.rstrip("/")


def invocation_info(item: dict[str, Any], name: str, affinity: str | None = None) -> dict[str, Any]:
    from inspire.services.serving.serving_output import public_serving

    detail = public_serving(item, fallback_name=name)
    endpoint = serving_endpoint(item)
    kind = detail.get("type", "")
    info: dict[str, Any] = {
        "name": detail.get("name", name),
        "status": detail.get("status", ""),
        "type": kind,
        "endpoint": endpoint,
        "credential_env": "INF_API_KEY",
        "auth_header": "Authorization",
        "auth_scheme": "Bearer",
        "affinity_header": "x-inspire-inference-key",
        "note": "An endpoint may remain assigned while the service is stopped; check readiness separately.",
    }
    if not endpoint:
        info["note"] = (
            "No supported credential-free endpoint is available yet. Retry serving status later."
        )
        return info
    target = endpoint
    if kind in {"EXCLUSIVE", "SERVERLESS"}:
        info["base_url"] = endpoint + "/v1"
        target += "/v1/chat/completions"
    else:
        info["note"] += (
            " Custom HTTP routes, methods and bodies are defined by your container; OpenAI compatibility is not assumed."
        )
    command = f'curl {shlex.quote(target)} \\\n  -H "Authorization: Bearer $INF_API_KEY"'
    if affinity is not None:
        command += " \\\n  -H " + shlex.quote("x-inspire-inference-key: " + affinity)
    if kind in {"EXCLUSIVE", "SERVERLESS"}:
        command += " \\\n  -H 'Content-Type: application/json' \\\n  -d " + shlex.quote(
            '{"model":"model","messages":[{"role":"user","content":"Hello"}]}'
        )
    info["example"] = command
    return info
