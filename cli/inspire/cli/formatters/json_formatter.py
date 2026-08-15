"""JSON output formatter for CLI commands.

Provides structured JSON output for machine-readable parsing.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from inspire.cli.utils.raw_ids import scrub_raw_ids


_CAMEL_ID_RE = re.compile(
    r"(^id$|Id$|Ids$|ID$|IDs$|Uuid$|Uuids$|UUID$|UUIDs$|Uid$|Uids$|"
    r"Handle$|Handles$)"
)
_ENGINEERING_KEYS = {
    "attempt",
    "attempts",
    "backend",
    "config_file",
    "config_files",
    "config_path",
    "debug",
    "endpoint",
    "env_file",
    "global_config_path",
    "http_method",
    "internal",
    "metadata",
    "method",
    "operation",
    "payload",
    "poll",
    "poll_interval",
    "progress",
    "project_account",
    "project_config_path",
    "project_shared",
    "raw",
    "request",
    "request_payload",
    "request_preview",
    "response",
    "response_metadata",
    "responsemetadata",
    "result",
    "retries",
    "retry",
    "scan",
    "scanned",
    "stack",
    "timing",
    "timings",
    "trace",
    "traceback",
    "verbose",
}
_ENGINEERING_SOURCE_VALUES = {"api", "browser", "cache", "live", "web"}
_SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "api_token",
    "auth_token",
    "client_secret",
    "login_name",
    "login_username",
    "password",
    "passwd",
    "refresh_token",
    "secret",
    "token",
    "username",
}
_RAW_CONTENT_KEYS = {
    "command_output",
    "content",
    "log_content",
    "output",
    "stderr",
    "stdout",
}
_INTERNAL_PATH_KEYS = {
    "backend_path",
    "debug_report",
    "debug_report_path",
    "executable_path",
    "internal_path",
    "internal_paths",
    "log_path",
    "runtime_path",
}
_URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b("
    r"access[_-]?token|account[_-]?id|api[_-]?key|"
    r"login(?:[_-]?(?:id|name|username))?|password|passwd|"
    r"refresh[_-]?token|token|user[_-]?id|username"
    r")"
    r"\s*[:=]\s*[^\s,;&]+"
)
_ALL_UNIX_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![\w:/])/"
    r"(?:[^/\s=:;,)\]}'\"]+/)*[^/\s=:;,)\]}'\"]+"
)
# Shared-storage roots the CLI is expected to name out loud: they are the
# addressing scheme for remote files, not local disk layout.
_SHARED_PATH_ROOTS = ("inspire", "shared", "workspace", "mnt", "data")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?<![\w])(?:[a-z]:\\)(?:[^\\\s=:;,)\]}'\"]+\\)+[^\\\s=:;,)\]}'\"]+"
)


def _is_id_key(key: object) -> bool:
    key_text = str(key or "")
    normalized = key_text.replace("-", "_").lower()
    if normalized in {
        "handle",
        "handles",
        "id",
        "ids",
        "uid",
        "uids",
        "uuid",
        "uuids",
    }:
        return True
    if normalized.endswith(
        (
            "_handle",
            "_handles",
            "_id",
            "_ids",
            "_uid",
            "_uids",
            "_uuid",
            "_uuids",
        )
    ):
        return True
    return bool(_CAMEL_ID_RE.search(key_text))


def _normalized_key(key: object) -> str:
    return str(key or "").replace("-", "_").strip().lower()


def _is_sensitive_field(key: object) -> bool:
    normalized = _normalized_key(key)
    compact = re.sub(r"[^a-z0-9]", "", str(key or "").lower())
    return (
        normalized in _SENSITIVE_KEYS
        or normalized.endswith("_token")
        or compact
        in {
            "accesstoken",
            "apikey",
            "apitoken",
            "authtoken",
            "clientsecret",
            "password",
            "passwd",
            "refreshtoken",
            "secret",
            "token",
        }
    )


def _is_engineering_field(key: object, value: Any) -> bool:
    normalized = _normalized_key(key)
    if normalized in _ENGINEERING_KEYS:
        return True
    return (
        normalized == "source"
        and isinstance(value, str)
        and value.strip().lower() in _ENGINEERING_SOURCE_VALUES
    )


def _sanitize_url(raw_url: str, *, redact: bool = False) -> str:
    trailing = ""
    while raw_url and raw_url[-1] in ".,;!?)]}":
        trailing = raw_url[-1] + trailing
        raw_url = raw_url[:-1]
    if redact:
        return "<redacted>" + trailing
    try:
        parsed = urlsplit(raw_url)
        hostname = parsed.hostname
        if not hostname:
            return raw_url + trailing
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        try:
            port = parsed.port
        except ValueError:
            port = None
        netloc = f"{hostname}:{port}" if port is not None else hostname
        clean = urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        return clean + trailing
    except ValueError:
        return "<redacted>" + trailing


def _sanitize_public_text(
    value: str,
    *,
    redact_paths: bool = False,
    redact_urls: bool = False,
    redact_platform_paths: bool = False,
) -> str:
    sanitized = scrub_raw_ids(value)
    sanitized = _URL_RE.sub(
        lambda match: _sanitize_url(match.group(0), redact=redact_urls),
        sanitized,
    )
    sanitized = _SENSITIVE_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}=<redacted>",
        sanitized,
    )
    if not redact_paths:
        return sanitized
    sanitized = _WINDOWS_ABSOLUTE_PATH_RE.sub("<redacted>", sanitized)
    if redact_platform_paths:
        return _ALL_UNIX_ABSOLUTE_PATH_RE.sub("<redacted>", sanitized)

    def _keep_shared_paths(match: re.Match[str]) -> str:
        # Decide per whole path, not per leading slash. Refusing to *match*
        # exempt roots left the rest of such a path exposed to a second
        # attempt: in `/inspire/<storage>/...` the scan resumed after `>` and
        # redacted the trailing `/...` on its own.
        path = match.group(0)
        root = path[1:].split("/", 1)[0]
        return path if root in _SHARED_PATH_ROOTS else "<redacted>"

    return _ALL_UNIX_ABSOLUTE_PATH_RE.sub(_keep_shared_paths, sanitized)


def sanitize_text(
    value: object,
    *,
    redact_paths: bool = False,
    redact_urls: bool = False,
    redact_platform_paths: bool = False,
) -> str:
    """Sanitize human-facing text with the same rules as JSON errors."""
    return _sanitize_public_text(
        str(value or ""),
        redact_paths=redact_paths,
        redact_urls=redact_urls,
        redact_platform_paths=redact_platform_paths,
    )


def _is_internal_path_field(key: object) -> bool:
    normalized = _normalized_key(key)
    return (
        normalized in _INTERNAL_PATH_KEYS
        or normalized.endswith("_internal_path")
        or normalized.endswith("_log_path")
        or normalized.endswith("_report_path")
    )


def _sanitize_json_value(
    value: Any,
    *,
    parent_key: object = "",
    preserve_path_keys: frozenset[str] = frozenset(),
    preserve_raw_keys: frozenset[str] = frozenset(),
) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize_json_value(
                child,
                parent_key=key,
                preserve_path_keys=preserve_path_keys,
                preserve_raw_keys=preserve_raw_keys,
            )
            for key, child in value.items()
            if not _is_id_key(key)
            and not _is_sensitive_field(key)
            and not _is_engineering_field(key, child)
        }
    if isinstance(value, list):
        return [
            _sanitize_json_value(
                item,
                parent_key=parent_key,
                preserve_path_keys=preserve_path_keys,
                preserve_raw_keys=preserve_raw_keys,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return [
            _sanitize_json_value(
                item,
                parent_key=parent_key,
                preserve_path_keys=preserve_path_keys,
                preserve_raw_keys=preserve_raw_keys,
            )
            for item in value
        ]
    if isinstance(value, str):
        if _normalized_key(parent_key) in preserve_raw_keys:
            # The caller declared this key's value *is* the answer, so it ships
            # byte for byte. Only for values that are useless once scrubbed —
            # `notebook proxy-url` is the one caller today, because a proxy URL
            # with its handles removed addresses nothing.
            return value
        if _normalized_key(parent_key) in _RAW_CONTENT_KEYS:
            return scrub_raw_ids(value)
        preserve_path = _normalized_key(parent_key) in preserve_path_keys
        if (
            not preserve_path
            and _is_internal_path_field(parent_key)
            and (value.startswith("/") or _WINDOWS_ABSOLUTE_PATH_RE.match(value))
        ):
            return "<redacted>"
        return _sanitize_public_text(
            value,
            redact_paths=not preserve_path,
            redact_urls=False,
        )
    return value


def sanitize_json_data(
    data: Any,
    *,
    preserve_paths: set[str] | frozenset[str] | None = None,
    preserve_raw: set[str] | frozenset[str] | None = None,
) -> Any:
    """Return a CLI-safe JSON payload with platform handle fields removed.

    ``preserve_paths`` exempts a key from filesystem-path redaction only.
    ``preserve_raw`` is the stronger, rarer opt-in: the key's string value skips
    sanitization entirely. Reach for it only when scrubbing would destroy the
    value's whole purpose, and say why at the call site.
    """
    normalized_paths = frozenset(
        _normalized_key(key) for key in (preserve_paths or ())
    )
    normalized_raw = frozenset(_normalized_key(key) for key in (preserve_raw or ()))
    return _sanitize_json_value(
        data,
        preserve_path_keys=normalized_paths,
        preserve_raw_keys=normalized_raw,
    )


def format_json(
    data: Any,
    success: bool = True,
    *,
    preserve_paths: set[str] | frozenset[str] | None = None,
    preserve_raw: set[str] | frozenset[str] | None = None,
) -> str:
    """Format data as JSON output.

    Args:
        data: Data to format (dict, list, or other JSON-serializable)
        success: Whether the operation was successful

    Returns:
        JSON string with standard wrapper
    """
    output = {
        "success": success,
        "data": sanitize_json_data(
            data, preserve_paths=preserve_paths, preserve_raw=preserve_raw
        ),
    }
    return json.dumps(output, ensure_ascii=False, separators=(",", ":"))


def format_json_error(
    error_type: str,
    message: str,
    code: int = 1,
    hint: Optional[str] = None,
    data: Any = None,
) -> str:
    """Format an error as JSON output.

    Args:
        error_type: Type of error (e.g., "ConfigError", "AuthenticationError")
        message: Error message
        code: Exit code
        hint: Optional hint for fixing the error

    Returns:
        JSON string with error details
    """
    error_data: Dict[str, Any] = {
        "type": error_type,
        "code": code,
        "message": _sanitize_public_text(
            message,
            redact_paths=True,
            redact_urls=True,
        ),
    }
    if hint:
        error_data["hint"] = _sanitize_public_text(
            hint,
            redact_paths=True,
            redact_urls=True,
        )

    output = {"success": False, "error": error_data}
    if data is not None:
        output["data"] = sanitize_json_data(data)
    return json.dumps(output, ensure_ascii=False, separators=(",", ":"))
