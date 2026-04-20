"""Tests for wait_for_notebook_running terminal state detection."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from inspire.platform.web.browser_api.notebooks import (
    NotebookFailedError,
    wait_for_notebook_running,
)


class _FakeSession:
    workspace_id = "ws-test"


def _make_detail(status: str, **extra: object) -> dict:
    d: dict = {"status": status, "notebook_id": "nb-test"}
    d.update(extra)
    return d


# -- NotebookFailedError unit tests ------------------------------------------


def test_error_message_basic():
    err = NotebookFailedError("nb-1", "FAILED", {"status": "FAILED"})
    assert "nb-1" in str(err)
    assert "FAILED" in str(err)
    assert err.notebook_id == "nb-1"
    assert err.status == "FAILED"
    assert err.events == ""


def test_error_message_with_sub_status():
    detail = {"status": "FAILED", "sub_status": "GPU_ALLOC_ERROR"}
    err = NotebookFailedError("nb-2", "FAILED", detail)
    assert "Sub-status: GPU_ALLOC_ERROR" in str(err)


def test_error_carries_events():
    err = NotebookFailedError("nb-3", "ERROR", {}, events="FailedScheduling: no GPUs")
    assert err.events == "FailedScheduling: no GPUs"


# -- wait_for_notebook_running behaviour tests --------------------------------


@patch("inspire.platform.web.browser_api.notebooks._try_fetch_events", return_value="")
@patch("inspire.platform.web.browser_api.notebooks.get_notebook_detail")
def test_raises_on_failed_status(mock_detail, mock_events):
    mock_detail.return_value = _make_detail("FAILED")
    with pytest.raises(NotebookFailedError) as exc_info:
        wait_for_notebook_running("nb-test", session=_FakeSession(), timeout=10)
    assert exc_info.value.status == "FAILED"


@patch("inspire.platform.web.browser_api.notebooks._try_fetch_events", return_value="")
@patch("inspire.platform.web.browser_api.notebooks.get_notebook_detail")
def test_raises_on_stopped_status(mock_detail, mock_events):
    mock_detail.return_value = _make_detail("STOPPED")
    with pytest.raises(NotebookFailedError) as exc_info:
        wait_for_notebook_running("nb-test", session=_FakeSession(), timeout=10)
    assert exc_info.value.status == "STOPPED"


@patch("inspire.platform.web.browser_api.notebooks._try_fetch_events", return_value="")
@patch("inspire.platform.web.browser_api.notebooks.get_notebook_detail")
def test_raises_on_error_status(mock_detail, mock_events):
    mock_detail.return_value = _make_detail("ERROR")
    with pytest.raises(NotebookFailedError) as exc_info:
        wait_for_notebook_running("nb-test", session=_FakeSession(), timeout=10)
    assert exc_info.value.status == "ERROR"


@patch("inspire.platform.web.browser_api.notebooks._try_fetch_events", return_value="")
@patch("inspire.platform.web.browser_api.notebooks.get_notebook_detail")
def test_raises_on_deleted_status(mock_detail, mock_events):
    mock_detail.return_value = _make_detail("DELETED")
    with pytest.raises(NotebookFailedError) as exc_info:
        wait_for_notebook_running("nb-test", session=_FakeSession(), timeout=10)
    assert exc_info.value.status == "DELETED"


@patch("inspire.platform.web.browser_api.notebooks.get_notebook_detail")
def test_returns_on_running(mock_detail):
    mock_detail.return_value = _make_detail("RUNNING")
    result = wait_for_notebook_running("nb-test", session=_FakeSession(), timeout=10)
    assert result["status"] == "RUNNING"


@patch("inspire.platform.web.browser_api.notebooks.time")
@patch("inspire.platform.web.browser_api.notebooks.get_notebook_detail")
def test_raises_timeout_for_pending(mock_detail, mock_time):
    mock_detail.return_value = _make_detail("PENDING")
    # Simulate time progression: first call returns 0, second returns past timeout
    mock_time.time.side_effect = [0, 0, 11]
    mock_time.sleep = lambda _: None
    with pytest.raises(TimeoutError):
        wait_for_notebook_running("nb-test", session=_FakeSession(), timeout=10)


@patch(
    "inspire.platform.web.browser_api.notebooks._try_fetch_events",
    return_value="FailedScheduling: nvml.Init error",
)
@patch("inspire.platform.web.browser_api.notebooks.get_notebook_detail")
def test_failed_includes_events(mock_detail, mock_events):
    mock_detail.return_value = _make_detail("FAILED")
    with pytest.raises(NotebookFailedError) as exc_info:
        wait_for_notebook_running("nb-test", session=_FakeSession(), timeout=10)
    assert exc_info.value.events == "FailedScheduling: nvml.Init error"
