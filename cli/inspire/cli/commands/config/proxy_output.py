"""Formatting helpers for effective proxy diagnostics."""

from __future__ import annotations

from typing import Any


def _service_line(label: str, details: object) -> str:
    data = details if isinstance(details, dict) else {}
    source = str(data.get("source") or "none")
    route = str(data.get("route") or "direct")
    proxy = (
        data.get("selected")
        or data.get("server")
        or data.get("https")
        or data.get("all")
        or data.get("http")
    )
    no_proxy = data.get("no_proxy")

    parts = [f"source={source}", f"route={route}"]
    if proxy:
        parts.append(f"configured_proxy={proxy}")
    if no_proxy in {"matched", "not_matched"}:
        parts.append(f"NO_PROXY={no_proxy}")
    return f"  {label.ljust(11)} " + " ".join(parts)


def format_effective_proxy_lines(summary: dict[str, Any]) -> list[str]:
    """Format a redacted effective-proxy summary for human CLI output."""
    lines = ["Effective runtime proxy:"]
    target = summary.get("target")
    if target:
        lines.append(f"  Target:      {target}")
    lines.extend(
        [
            _service_line("Requests:", summary.get("requests")),
            _service_line("Playwright:", summary.get("playwright")),
            _service_line("rtunnel:", summary.get("rtunnel")),
        ]
    )
    return lines
