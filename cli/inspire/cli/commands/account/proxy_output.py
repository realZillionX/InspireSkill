"""Formatting helpers for effective proxy diagnostics."""

from __future__ import annotations

from typing import Any


_PROXY_SERVICES = ("requests", "playwright", "rtunnel")
_PROXY_SOURCES = {
    "explicit_env",
    "none",
    "requests:explicit_env",
    "requests:system_env",
    "requests:toml",
    "system_env",
    "toml",
}
_PROXY_ROUTES = {"direct", "proxy"}
_NO_PROXY_STATES = {"matched", "not_applicable", "not_matched", "not_set"}


def _public_service_summary(details: object) -> dict[str, str]:
    data = details if isinstance(details, dict) else {}
    source = str(data.get("source") or "none")
    route = str(data.get("route") or "direct")
    public = {
        "source": source if source in _PROXY_SOURCES else "unknown",
        "route": route if route in _PROXY_ROUTES else "unknown",
    }
    no_proxy = str(data.get("no_proxy") or "")
    if no_proxy in _NO_PROXY_STATES:
        public["no_proxy"] = no_proxy
    return public


def public_effective_proxy_summary(summary: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Project proxy diagnostics without target hosts, URLs, ports, or credentials."""
    return {
        service: _public_service_summary(summary.get(service))
        for service in _PROXY_SERVICES
    }


def _service_line(label: str, details: object) -> str:
    data = details if isinstance(details, dict) else {}
    source = str(data.get("source") or "none")
    route = str(data.get("route") or "direct")
    no_proxy = data.get("no_proxy")

    parts = [f"source={source}", f"route={route}"]
    if no_proxy in {"matched", "not_matched"}:
        parts.append(f"NO_PROXY={no_proxy}")
    return f"  {label.ljust(11)} " + " ".join(parts)


def format_effective_proxy_lines(summary: dict[str, Any]) -> list[str]:
    """Format a redacted effective-proxy summary for human CLI output."""
    public = public_effective_proxy_summary(summary)
    lines = ["Effective runtime proxy:"]
    lines.extend(
        [
            _service_line("Requests:", public.get("requests")),
            _service_line("Playwright:", public.get("playwright")),
            _service_line("rtunnel:", public.get("rtunnel")),
        ]
    )
    return lines
