from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

import pytest

from inspire.platform.web.session.models import WebSession


@pytest.fixture
def session_cache_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    cache_file = fake_home / ".inspire" / "accounts" / "alice" / "web_session.json"
    cache_file.parent.mkdir(parents=True)
    return cache_file


_RECENT = time.time()


@pytest.mark.parametrize("allow_expired", [False, True])
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(None, id="null-root"),
        pytest.param([], id="list-root"),
        pytest.param("stale", id="string-root"),
        pytest.param(42, id="number-root"),
        pytest.param(True, id="boolean-root"),
        pytest.param({}, id="missing-created-at"),
        pytest.param({"created_at": None}, id="null-created-at"),
        pytest.param({"created_at": "1"}, id="string-created-at"),
        pytest.param({"created_at": []}, id="list-created-at"),
        pytest.param({"created_at": True}, id="boolean-created-at"),
        pytest.param({"created_at": math.nan}, id="nan-created-at"),
        pytest.param({"created_at": math.inf}, id="infinite-created-at"),
        pytest.param({"created_at": 10**400}, id="oversized-created-at"),
        pytest.param({"created_at": _RECENT, "storage_state": []}, id="list-storage-state"),
        pytest.param(
            {"created_at": _RECENT, "storage_state": {"cookies": "stale"}},
            id="string-storage-cookies",
        ),
        pytest.param(
            {
                "created_at": _RECENT,
                "storage_state": {"cookies": [{"name": "session"}]},
            },
            id="missing-cookie-value",
        ),
        pytest.param(
            {
                "created_at": _RECENT,
                "storage_state": {"cookies": [{"name": "session", "value": "ok", "secure": 1}]},
            },
            id="non-boolean-cookie-flag",
        ),
        pytest.param(
            {
                "created_at": _RECENT,
                "storage_state": {"origins": [{"localStorage": []}]},
            },
            id="missing-origin",
        ),
        pytest.param(
            {
                "created_at": _RECENT,
                "storage_state": {
                    "origins": [{"origin": "https://example.test", "localStorage": "stale"}]
                },
            },
            id="string-origin-local-storage",
        ),
        pytest.param(
            {
                "created_at": _RECENT,
                "all_workspace_ids": ["ws-test", 1],
            },
            id="non-string-workspace-id",
        ),
        pytest.param(
            {"created_at": _RECENT, "all_workspace_fair_scheduling": {"ws-test": "yes"}},
            id="non-boolean-workspace-capability",
        ),
        pytest.param(
            {"created_at": _RECENT, "cookies": {"session": 1}}, id="unsupported-top-level-cookies"
        ),
    ],
)
def test_load_treats_malformed_payload_as_cache_miss(
    session_cache_file: Path,
    payload: Any,
    allow_expired: bool,
) -> None:
    session_cache_file.write_text(json.dumps(payload), encoding="utf-8")

    assert WebSession.load(account="alice", allow_expired=allow_expired) is None


@pytest.mark.parametrize("raw", [b"{", b"\xff"])
def test_load_treats_unreadable_payload_as_cache_miss(
    session_cache_file: Path,
    raw: bytes,
) -> None:
    session_cache_file.write_bytes(raw)

    assert WebSession.load(account="alice") is None


def test_load_accepts_valid_payload_and_normalizes_optional_storage_fields(
    session_cache_file: Path,
) -> None:
    payload = {
        "created_at": time.time(),
        "storage_state": {
            "cookies": [
                {
                    "name": "session",
                    "value": "secret",
                    "domain": ".example.test",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ]
        },
        "workspace_id": "ws-test",
        "all_workspace_ids": ["ws-test"],
        "all_workspace_names": {"ws-test": "Test"},
        "all_workspace_fair_scheduling": {"ws-test": True},
    }
    session_cache_file.write_text(json.dumps(payload), encoding="utf-8")

    session = WebSession.load(account="alice")

    assert session is not None
    assert session.account == "alice"
    assert session.workspace_id == "ws-test"
    assert session.storage_state["origins"] == []
    assert session.cookies == {"session": "secret"}
    assert session.all_workspace_fair_scheduling == {"ws-test": True}


def test_load_rejects_cookie_only_cache(session_cache_file: Path) -> None:
    payload = {
        "created_at": time.time(),
        "cookies": {"session": "secret"},
    }
    session_cache_file.write_text(json.dumps(payload), encoding="utf-8")

    assert WebSession.load(account="alice") is None


def test_load_preserves_expired_cache_behavior(session_cache_file: Path) -> None:
    payload = {"created_at": 0, "storage_state": {"cookies": []}}
    session_cache_file.write_text(json.dumps(payload), encoding="utf-8")

    assert WebSession.load(account="alice") is None
    assert WebSession.load(account="alice", allow_expired=True) is not None


def test_load_does_not_hide_deserialization_errors(
    session_cache_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"created_at": time.time(), "storage_state": {"cookies": []}}
    session_cache_file.write_text(json.dumps(payload), encoding="utf-8")

    def fail_from_dict(cls: type[WebSession], data: dict[str, Any]) -> WebSession:
        raise RuntimeError("deserialization bug")

    monkeypatch.setattr(WebSession, "from_dict", classmethod(fail_from_dict))

    with pytest.raises(RuntimeError, match="deserialization bug"):
        WebSession.load(account="alice")
