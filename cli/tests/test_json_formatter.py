from __future__ import annotations

import json

from inspire.cli.formatters.json_formatter import (
    format_json,
    format_json_error,
    sanitize_json_data,
    sanitize_text,
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
            "username": "login-secret",
            "login_name": "login-secret",
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


def test_sanitizer_removes_secrets_and_internal_transport_details() -> None:
    payload = sanitize_json_data(
        {
            "name": "train",
            "token": "secret-token",
            "password": "secret-password",
            "nested": {
                "access_token": "secret-access-token",
                "refreshToken": "secret-refresh-token",
                "url": "https://alice:secret@example.test/ide?token=secret#fragment",
                "internal_path": "/Users/alice/.inspire/runtime.sock",
                "path": "/shared/output/model",
            },
        }
    )

    assert payload == {
        "name": "train",
        "nested": {
            "url": "https://example.test/ide",
            "internal_path": "<redacted>",
            "path": "/shared/output/model",
        },
    }


def test_sanitizer_preserves_explicit_remote_raw_content() -> None:
    content = (
        "https://alice:secret@example.test/run?token=keep "
        "path=/home/user/out.log token=keep"
    )

    assert sanitize_json_data({"content": content, "output": content}) == {
        "content": content,
        "output": content,
    }


def test_sanitizer_redacts_local_paths_from_default_success_output() -> None:
    payload = sanitize_json_data(
        {
            "message": (
                "Saved /Users/alice/private/model.pt from "
                "https://alice:secret@example.test/run?token=abc#fragment"
            ),
            "path": "/home/alice/results/model.pt",
            "remote_path": "/inspire/hdd/project/model.pt",
            "shared_path": "/shared/output/model.pt",
        }
    )

    assert payload == {
        "message": "Saved <redacted> from https://example.test/run",
        "path": "<redacted>",
        "remote_path": "/inspire/hdd/project/model.pt",
        "shared_path": "/shared/output/model.pt",
    }


def test_sanitizer_preserve_paths_is_an_explicit_path_opt_in() -> None:
    assert sanitize_json_data(
        {"log_path": "/Users/alice/logs/train.log"},
        preserve_paths={"log_path"},
    ) == {"log_path": "/Users/alice/logs/train.log"}


_PROXY_URL = (
    "https://nat2-notebook-inspire.sii.edu.cn"
    "/ws-6e6ba362-e98e-45b2-9c5a-311998e93d65"
    "/project-84370d52-6e91-4911-8116-5840a97e984c"
    "/user-263239cf-402f-4ae0-a8e2-2fcca034026c"
    "/vscode/bae66d5d-8423-4730-aa06-96a770748109"
    "/ed659e4b-012e-4d94-9439-c67eebc771d5/proxy/30000/"
)


def test_sanitizer_scrubs_handle_bearing_urls_by_default() -> None:
    scrubbed = sanitize_json_data({"url": _PROXY_URL})["url"]

    assert "<redacted>" in scrubbed
    assert "ws-6e6ba362-e98e-45b2-9c5a-311998e93d65" not in scrubbed


def test_sanitizer_preserve_raw_ships_the_value_byte_for_byte() -> None:
    """The stronger opt-in, for values that scrubbing would render useless.

    `notebook proxy-url` is the one caller: every segment of a proxy URL is a
    platform handle, so the default scrub leaves an address reaching nothing.
    `preserve_paths` is not enough — it only exempts filesystem paths, while
    handle redaction runs unconditionally.
    """
    assert sanitize_json_data({"url": _PROXY_URL}, preserve_raw={"url"}) == {
        "url": _PROXY_URL
    }
    # Narrow: the opt-in names one key and does not leak to its neighbours.
    mixed = sanitize_json_data(
        {"url": _PROXY_URL, "other": _PROXY_URL}, preserve_raw={"url"}
    )
    assert mixed["url"] == _PROXY_URL
    assert "<redacted>" in mixed["other"]


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


def test_error_json_removes_credentials_urls_and_absolute_paths() -> None:
    rendered = format_json_error(
        "SSHExecutionError",
        "SSH failed: https://user:pass@host.test/run?access_token=abc "
        "path=/home/user/run.log password=hunter2",
    )

    assert json.loads(rendered) == {
        "success": False,
        "error": {
            "type": "SSHExecutionError",
            "code": 1,
            "message": (
                "SSH failed: <redacted> "
                "path=<redacted> password=<redacted>"
            ),
        },
    }
    for secret in ("user:pass", "access_token=abc", "/home/user/run.log", "hunter2"):
        assert secret not in rendered


def test_error_text_redacts_platform_paths_urls_and_login_assignments() -> None:
    rendered = sanitize_text(
        "request https://internal.example/api failed at /inspire/private/run.log "
        "login_name=alice account_id=acct-123",
        redact_paths=True,
        redact_urls=True,
        redact_platform_paths=True,
    )

    assert rendered == (
        "request <redacted> failed at <redacted> "
        "login_name=<redacted> account_id=<redacted>"
    )


def test_final_output_guard_scrubs_text_and_bytes() -> None:
    raw = "job-12345678-1234-1234-1234-123456789abc"

    assert sanitize_output_message(raw) == "<redacted>"
    assert sanitize_output_message(raw.encode()) == b"<redacted>"
    assert sanitize_output_message("invalid job id deadbeef") == (
        "invalid job id <redacted>"
    )
    assert sanitize_output_message("commit deadbeef") == "commit deadbeef"


def test_final_output_guard_keeps_short_and_name_shaped_values() -> None:
    """``lcg-1``/``cg-1``/``ws-1`` are names here, not handles.

    Redacting a two-character numeric suffix cost far more names than it ever
    caught handles, and a redacted name is unusable in a name-only CLI.
    """
    raw = "lcg-1 cg-1 ws-1 | group-a cg-alpha workspace-1-name"

    assert sanitize_output_message(raw) == raw
    assert sanitize_output_message(raw.encode()) == raw.encode()
