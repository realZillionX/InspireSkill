from __future__ import annotations

import json

from inspire.cli.formatters.json_formatter import (
    format_json,
    format_json_error,
    sanitize_json_data,
)
from inspire.cli.utils.output_guard import sanitize_output_message


def test_sanitizer_removes_handles_and_engineering_metadata_recursively() -> None:
    payload = sanitize_json_data(
        {
            "name": "train",
            "job_id": "job-secret",
            "resourceHandle": "job-secret",
            "object_uuid": "uuid-secret",
            "ownerUid": "user-secret",
            "source": "web",
            "backend": "browser",
            "endpoint": "/api/jobs/list",
            "attempts": 3,
            "timing": {"lookup_ms": 42},
            "result": {"id": "nested-secret", "status": "ok"},
            "items": [
                {
                    "name": "worker",
                    "instanceId": "instance-secret",
                    "raw": {"large": "payload"},
                    "status": "RUNNING",
                }
            ],
        }
    )

    assert payload == {
        "name": "train",
        "items": [{"name": "worker", "status": "RUNNING"}],
    }


def test_sanitizer_retains_business_source_values() -> None:
    assert sanitize_json_data(
        {
            "name": "pytorch",
            "source": "SOURCE_OFFICIAL",
        }
    ) == {
        "name": "pytorch",
        "source": "SOURCE_OFFICIAL",
    }


def test_success_json_is_compact_and_keeps_stable_envelope() -> None:
    rendered = format_json({"name": "train", "status": "RUNNING"})

    assert "\n" not in rendered
    assert ": " not in rendered
    assert json.loads(rendered) == {
        "success": True,
        "data": {"name": "train", "status": "RUNNING"},
    }


def test_sanitizer_keeps_actionable_empty_business_values() -> None:
    assert sanitize_json_data(
        {
            "name": "train",
            "events": [],
            "message": None,
            "metadata": {},
        }
    ) == {
        "name": "train",
        "events": [],
        "message": None,
    }


def test_error_json_is_compact_and_scrubs_platform_handles() -> None:
    rendered = format_json_error(
        "NotFound",
        "Missing job-12345678-1234-1234-1234-123456789abc",
        12,
        hint="List by name.",
    )

    assert "\n" not in rendered
    assert "job-12345678-1234-1234-1234-123456789abc" not in rendered
    assert json.loads(rendered) == {
        "success": False,
        "error": {
            "type": "NotFound",
            "code": 12,
            "message": "Missing <redacted>",
            "hint": "List by name.",
        },
    }


def test_final_output_guard_scrubs_text_and_bytes() -> None:
    raw = "job-12345678-1234-1234-1234-123456789abc"

    assert sanitize_output_message(raw) == "<redacted>"
    assert sanitize_output_message(raw.encode()) == b"<redacted>"
