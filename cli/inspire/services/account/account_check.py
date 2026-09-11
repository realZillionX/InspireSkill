"""Credential validation and placeholder host diagnostics."""

from __future__ import annotations

from urllib.parse import urlsplit

from inspire.config import Config, ConfigError, SOURCE_DEFAULT


_PLACEHOLDER_HOSTS = {
    "api.example.com",
    "example.com",
    "example.org",
    "example.net",
}


_PLACEHOLDER_HOST_SUFFIXES = (
    ".example.com",
    ".example.org",
    ".example.net",
)


_HOST_VALIDATION_FIELDS = (("base_url", "INSPIRE_BASE_URL"),)


def _extract_hostname(value: str | None) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    if not text or text.startswith("/"):
        return None

    if "://" in text:
        parsed = urlsplit(text)
        return parsed.hostname.lower() if parsed.hostname else None

    if text.startswith("//"):
        parsed = urlsplit(f"https:{text}")
        return parsed.hostname.lower() if parsed.hostname else None

    candidate = text.split("/", 1)[0].strip()
    if not candidate or " " in candidate:
        return None
    if "@" in candidate:
        candidate = candidate.rsplit("@", 1)[-1]
    if ":" in candidate:
        candidate = candidate.split(":", 1)[0]
    if "." not in candidate:
        return None
    return candidate.lower()


def _is_placeholder_host(host: str) -> bool:
    if host in _PLACEHOLDER_HOSTS:
        return True
    return any(host.endswith(suffix) for suffix in _PLACEHOLDER_HOST_SUFFIXES)


def find_placeholder_host_issues(cfg: Config, sources: dict[str, str]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    for field_name, env_var in _HOST_VALIDATION_FIELDS:
        raw_value = getattr(cfg, field_name, None)
        if raw_value in (None, ""):
            continue

        value = str(raw_value)
        host = _extract_hostname(value)
        if not host:
            continue
        if not _is_placeholder_host(host):
            continue

        issues.append(
            {
                "field": field_name,
                "env_var": env_var,
                "value": value,
                "host": host,
                "source": sources.get(field_name, SOURCE_DEFAULT),
            }
        )
    return issues


def format_placeholder_issue_message(issues: list[dict[str, str]]) -> str:
    # Name the matched host, not the raw value: the value is a full URL and
    # gets scrubbed to `<redacted>` on its way out, which left the user staring
    # at an error that would not say what was wrong. The matched host is one of
    # a fixed set of documentation placeholders and is safe to print.
    lines = ["Placeholder host values detected in configuration:"]
    for issue in issues:
        lines.append(
            f"  - {issue['env_var']} points at {issue['host']} [source: {issue['source']}]"
        )
    lines.append("Use real host values in config files or environment variables.")
    lines.append("Path-only API prefixes are allowed.")
    return "\n".join(lines)


def validate_required_credentials(cfg: Config) -> None:
    if not cfg.username or not cfg.password:
        raise ConfigError(
            "Missing platform credentials. Run `inspire account add <name>` to "
            "configure them; the active account's `[auth]` block is the only "
            "supported source."
        )
