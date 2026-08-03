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
            {"created_at": _RECENT, "storage_state": "stale"},
            id="string-storage-state",
        ),
        pytest.param(
            {"created_at": _RECENT, "storage_state": {"cookies": "stale"}},
            id="string-storage-cookies",
        ),
        pytest.param(
            {"created_at": _RECENT, "storage_state": {"cookies": ["stale"]}},
            id="non-object-storage-cookie",
        ),
        pytest.param(
            {"created_at": _RECENT, "storage_state": {"origins": "stale"}},
            id="string-storage-origins",
        ),
        pytest.param(
            {"created_at": _RECENT, "storage_state": {"origins": ["stale"]}},
            id="non-object-storage-origin",
        ),
        pytest.param({"created_at": _RECENT, "cookies": []}, id="list-legacy-cookies"),
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


def test_load_accepts_valid_payload(session_cache_file: Path) -> None:
    payload = {
        "created_at": time.time(),
        "storage_state": {
            "cookies": [{"name": "session", "value": "secret"}],
            "origins": [],
        },
        "workspace_id": "ws-test",
    }
    session_cache_file.write_text(json.dumps(payload), encoding="utf-8")

    session = WebSession.load(account="alice")

    assert session is not None
    assert session.account == "alice"
    assert session.workspace_id == "ws-test"


def test_load_preserves_legacy_cookie_cache(session_cache_file: Path) -> None:
    payload = {
        "created_at": time.time(),
        "cookies": {"session": "secret"},
    }
    session_cache_file.write_text(json.dumps(payload), encoding="utf-8")

    session = WebSession.load(account="alice")

    assert session is not None
    assert session.cookies == {"session": "secret"}
    assert session.storage_state == {"cookies": [], "origins": []}


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
