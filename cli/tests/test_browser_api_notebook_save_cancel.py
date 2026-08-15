"""Browser API tests for the notebook image-save side actions.

``CancelSaveMirror`` and ``CheckNotebook`` both fold exactly one platform
refusal into a return value and raise on everything else, so each has a test on
the failure side as well as the answer side.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from inspire.platform.web.browser_api import notebooks as notebooks_module
from inspire.platform.web.browser_api.notebooks import (
    cancel_notebook_image_save,
    notebook_name_exists,
    save_notebook_as_image,
)
from inspire.platform.web.session import TransientAPIError


class _FakeSession:
    workspace_id = "ws-default"


def _install_response(
    monkeypatch: pytest.MonkeyPatch, response: dict, record: dict
) -> None:
    def _fake(session, method, url, *, referer=None, body=None, timeout=30, **kwargs):
        record["method"] = method
        record["url"] = url
        record["referer"] = referer
        record["body"] = body
        record["timeout"] = timeout
        return response

    monkeypatch.setattr(notebooks_module, "_request_json", _fake)


def _install_error(monkeypatch: pytest.MonkeyPatch, code: str, message: str) -> None:
    _install_response(
        monkeypatch,
        {"ResponseMetadata": {"Error": {"Code": code, "Message": message}}},
        {},
    )


# ---------------------------------------------------------------------------
# CancelSaveMirror
# ---------------------------------------------------------------------------


def test_cancel_notebook_image_save_sends_only_the_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record: dict[str, Any] = {}
    _install_response(monkeypatch, {"ResponseMetadata": {}, "Result": None}, record)

    assert cancel_notebook_image_save("nb-1", session=_FakeSession()) is True

    assert record["method"] == "POST"
    assert record["url"] == "/api/v2/notebook?Action=CancelSaveMirror"
    # The Action shares SaveNotebookImage's request message, but the notebook
    # alone selects the save in flight — name/version must not be invented here.
    assert record["body"] == {"notebook_id": "nb-1"}


def test_cancel_notebook_image_save_reports_no_save_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_error(
        monkeypatch,
        "Conflict",
        "Save image demo:v1 of notebook nb-secret-1 is already finished "
        "(status 2), nothing to cancel",
    )

    assert cancel_notebook_image_save("nb-secret-1", session=_FakeSession()) is False


def test_cancel_notebook_image_save_raises_on_other_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_error(monkeypatch, "ResourceNotFound", "notebook not found")

    with pytest.raises(ValueError):
        cancel_notebook_image_save("nb-missing", session=_FakeSession())


def test_cancel_notebook_image_save_propagates_transient_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A platform that never answered must not read as "nothing was running"."""
    _install_error(monkeypatch, "ServiceUnavailable", "nothing to cancel")

    with pytest.raises(TransientAPIError):
        cancel_notebook_image_save("nb-1", session=_FakeSession())


def test_cancel_notebook_image_save_rejects_empty_handle() -> None:
    with pytest.raises(ValueError):
        cancel_notebook_image_save("  ", session=_FakeSession())


# ---------------------------------------------------------------------------
# CheckNotebook
# ---------------------------------------------------------------------------


def test_notebook_name_exists_reads_the_returned_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record: dict[str, Any] = {}
    _install_response(
        monkeypatch,
        {
            "ResponseMetadata": {},
            "Result": {"notebook_id": "nb-9", "sub_code": 0, "sub_msg": ""},
        },
        record,
    )

    assert notebook_name_exists("demo", workspace_id="ws-1", session=_FakeSession())

    assert record["url"] == "/api/v2/notebook?Action=CheckNotebook"
    assert record["body"] == {"name": "demo", "workspace_id": "ws-1"}


def test_notebook_name_exists_reads_an_empty_result_as_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_response(monkeypatch, {"ResponseMetadata": {}, "Result": None}, {})

    assert not notebook_name_exists("demo", workspace_id="ws-1", session=_FakeSession())


@pytest.mark.parametrize(
    ("name", "workspace_id"),
    [("", "ws-1"), ("   ", "ws-1"), ("demo", ""), ("demo", "   ")],
)
def test_notebook_name_exists_requires_both_halves_of_the_scope(
    monkeypatch: pytest.MonkeyPatch, name: str, workspace_id: str
) -> None:
    """The Action answers "free" for an unscoped question, so it is never sent."""
    called: dict[str, Any] = {}

    def _fail(*_args, **_kwargs):
        called["sent"] = True
        raise AssertionError("request must not be sent")

    monkeypatch.setattr(notebooks_module, "_request_json", _fail)

    with pytest.raises(ValueError):
        notebook_name_exists(name, workspace_id=workspace_id, session=_FakeSession())
    assert "sent" not in called


def test_notebook_name_exists_raises_instead_of_answering_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_error(monkeypatch, "AccessForbidden", "Access denied")

    with pytest.raises(ValueError):
        notebook_name_exists("demo", workspace_id="ws-1", session=_FakeSession())


def test_save_notebook_as_image_posts_the_notebook_action(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, Any] = {}

    def fake_notebook_v2(session, action: str, body: Optional[dict] = None, *, timeout: int = 30) -> Any:
        captured["action"] = action
        captured["body"] = body
        captured["timeout"] = timeout
        # The platform answers `Result: null` here, which unwraps to {}.
        return {}

    monkeypatch.setattr(notebooks_module, "_notebook_v2", fake_notebook_v2)
    monkeypatch.setattr(
        notebooks_module,
        "_get_session_and_workspace_id",
        lambda workspace_id, session: (_FakeSession(), "ws-test"),
    )

    result = save_notebook_as_image(
        notebook_id="nb-1",
        name="demo",
        version="v2",
        description="saved",
    )

    # The save lives on the `notebook` service, not `image`.
    assert captured["action"] == "SaveNotebookImage"
    assert captured["timeout"] == 60
    # `visibility` is rejected by the platform; callers use update_image instead.
    assert captured["body"] == {
        "notebook_id": "nb-1",
        "name": "demo",
        "version": "v2",
        "description": "saved",
    }
    # No image id comes back, so the command layer has to find it by listing.
    assert result == {}
