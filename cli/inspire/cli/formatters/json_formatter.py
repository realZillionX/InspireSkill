"""JSON output formatter for CLI commands.

Provides structured JSON output for machine-readable parsing.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from inspire.cli.utils.raw_ids import scrub_raw_ids


_CAMEL_ID_RE = re.compile(
    r"(^id$|Id$|Ids$|ID$|IDs$|Uuid$|Uuids$|UUID$|UUIDs$|Uid$|Uids$|"
    r"Handle$|Handles$)"
)
_ENGINEERING_KEYS = {
    "debug",
    "internal",
    "metadata",
    "method",
    "payload",
    "progress",
    "raw",
    "request",
    "request_payload",
    "request_preview",
    "response",
    "response_metadata",
    "responsemetadata",
    "result",
    "scan",
    "scanned",
    "stack",
    "trace",
    "traceback",
}
_ENGINEERING_SOURCE_VALUES = {"api", "browser", "cache", "live", "web"}


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


def _is_engineering_field(key: object, value: Any) -> bool:
    normalized = _normalized_key(key)
    if normalized in _ENGINEERING_KEYS:
        return True
    return (
        normalized == "source"
        and isinstance(value, str)
        and value.strip().lower() in _ENGINEERING_SOURCE_VALUES
    )


def _sanitize_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize_json_value(child)
            for key, child in value.items()
            if not _is_id_key(key) and not _is_engineering_field(key, child)
        }
    if isinstance(value, list):
        return [_sanitize_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_json_value(item) for item in value]
    if isinstance(value, str):
        return scrub_raw_ids(value)
    return value


def sanitize_json_data(data: Any) -> Any:
    """Return a CLI-safe JSON payload with platform handle fields removed."""
    return _sanitize_json_value(data)


def format_json(data: Any, success: bool = True) -> str:
    """Format data as JSON output.

    Args:
        data: Data to format (dict, list, or other JSON-serializable)
        success: Whether the operation was successful

    Returns:
        JSON string with standard wrapper
    """
    output = {"success": success, "data": sanitize_json_data(data)}
    return json.dumps(output, ensure_ascii=False, separators=(",", ":"))


def format_json_error(
    error_type: str, message: str, code: int = 1, hint: Optional[str] = None
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
        "message": scrub_raw_ids(message),
    }
    if hint:
        error_data["hint"] = scrub_raw_ids(hint)

    output = {"success": False, "error": error_data}
    return json.dumps(output, ensure_ascii=False, separators=(",", ":"))
