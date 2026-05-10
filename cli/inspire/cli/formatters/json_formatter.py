"""JSON output formatter for CLI commands.

Provides structured JSON output for machine-readable parsing.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from inspire.cli.utils.raw_ids import scrub_raw_ids


_CAMEL_ID_RE = re.compile(r"(^id$|Id$|Ids$|ID$|IDs$)")


def _is_id_key(key: object) -> bool:
    key_text = str(key or "")
    normalized = key_text.replace("-", "_").lower()
    if normalized in {"id", "ids"}:
        return True
    if normalized.endswith("_id") or normalized.endswith("_ids"):
        return True
    return bool(_CAMEL_ID_RE.search(key_text))


def _sanitize_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize_json_value(child)
            for key, child in value.items()
            if not _is_id_key(key)
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


def format_json(data: Any, success: bool = True, *, allow_ids: bool = False) -> str:
    """Format data as JSON output.

    Args:
        data: Data to format (dict, list, or other JSON-serializable)
        success: Whether the operation was successful

    Returns:
        JSON string with standard wrapper
    """
    output = {"success": success, "data": data if allow_ids else sanitize_json_data(data)}
    return json.dumps(output, indent=2, ensure_ascii=False)


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
    return json.dumps(output, indent=2, ensure_ascii=False)
