"""Unit tests for user-center SSH key Browser API wrappers."""

from __future__ import annotations

from typing import Any

import pytest

from inspire.platform.web.browser_api import users as users_module
from inspire.platform.web.browser_api.users import (
    create_user_ssh_key,
    delete_user_ssh_key,
    get_user_detail,
    list_user_ssh_keys,
)


class _FakeSession:
    workspace_id = "ws-default"


def test_user_api_validation_uses_visible_selection_terms() -> None:
    with pytest.raises(ValueError, match="User selection is required\\."):
        get_user_detail("", session=_FakeSession())

    with pytest.raises(ValueError, match="SSH key selection is required\\."):
        delete_user_ssh_key("", session=_FakeSession())


def _install_fake_request(
    monkeypatch: pytest.MonkeyPatch, response: dict, record: dict[str, Any]
) -> None:
    def _fake(session, method, url, *, referer=None, body=None, timeout=30, **kwargs):
        record["session"] = session
        record["method"] = method
        record["url"] = url
        record["referer"] = referer
        record["body"] = body
        record["timeout"] = timeout
        return response

    monkeypatch.setattr(users_module, "_request_json", _fake)


def test_list_user_ssh_keys_posts_expected_body(monkeypatch: pytest.MonkeyPatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "code": 0,
            "data": {
                "list": [
                    {
                        "id": "ssh-12345678-1234-1234-1234-123456789abc",
                        "name": "main-key",
                        "fingerprint": "SHA256:abc",
                    }
                ],
                "total": "1",
            },
        },
        record,
    )

    items, total = list_user_ssh_keys(page=2, page_size=20, session=_FakeSession())

    assert total == 1
    assert items[0]["name"] == "main-key"
    assert record["method"] == "POST"
    assert record["url"].endswith("/ssh/list")
    assert record["referer"].endswith("/userCenter?tab=sshkey")
    assert record["body"] == {"page": 2, "page_size": 20}


def test_get_user_detail_uses_rest_path(monkeypatch: pytest.MonkeyPatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {
            "code": 0,
            "data": {
                "id": "user-12345678-1234-1234-1234-123456789abc",
                "name": "Alice",
            },
        },
        record,
    )

    user = get_user_detail(
        "user-12345678-1234-1234-1234-123456789abc",
        session=_FakeSession(),
    )

    assert user["name"] == "Alice"
    assert record["method"] == "GET"
    assert record["url"].endswith("/user/user-12345678-1234-1234-1234-123456789abc")
    assert record["body"] is None


def test_create_user_ssh_key_uses_content_field(monkeypatch: pytest.MonkeyPatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(
        monkeypatch,
        {"code": 0, "data": {"ssh_id": "ssh-12345678-1234-1234-1234-123456789abc"}},
        record,
    )

    result = create_user_ssh_key(
        name="main-key",
        content="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample user@example",
        session=_FakeSession(),
    )

    assert result["ssh_id"].startswith("ssh-")
    assert record["method"] == "POST"
    assert record["url"].endswith("/ssh/create")
    assert record["body"] == {
        "name": "main-key",
        "content": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample user@example",
    }
    assert "public_key" not in record["body"]
    assert "key" not in record["body"]
    assert "ssh_key" not in record["body"]


def test_delete_user_ssh_key_uses_rest_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    record: dict[str, Any] = {}
    _install_fake_request(monkeypatch, {"code": 0, "data": {}}, record)

    delete_user_ssh_key("ssh-12345678-1234-1234-1234-123456789abc", session=_FakeSession())

    assert record["method"] == "DELETE"
    assert record["url"].endswith("/ssh/ssh-12345678-1234-1234-1234-123456789abc")
    assert record["body"] is None


def test_user_ssh_key_wrappers_raise_on_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_request(monkeypatch, {"code": 100002, "message": "bad request"}, {})

    with pytest.raises(ValueError, match="bad request"):
        list_user_ssh_keys(session=_FakeSession())
