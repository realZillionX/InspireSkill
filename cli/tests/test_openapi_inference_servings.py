"""Unit tests for `inspire.platform.openapi.inference_servings`.

The three wrapped endpoints (create / detail / stop) are Bearer-authenticated
and have no natural `--dry-run`, so exercising them live requires standing up
a real inference serving on qz.sii.edu.cn. These tests instead pin the
wire-format contract (payload shape, code==0 happy path, code!=0 → InspireAPIError)
and lock down the `_coerce_int` bool/float rejection that Codex flagged.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from inspire.platform.openapi.errors import InspireAPIError
from inspire.platform.openapi.inference_servings import (
    create_inference_serving,
    get_inference_serving_detail,
    stop_inference_serving,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _DummyAPI:
    """Minimal API-like object matching what create/detail/stop poke at."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []
        self.endpoints = SimpleNamespace(
            INFERENCE_SERVING_CREATE="/openapi/v1/inference_servings/create",
            INFERENCE_SERVING_DETAIL="/openapi/v1/inference_servings/detail",
            INFERENCE_SERVING_STOP="/openapi/v1/inference_servings/stop",
        )
        self.last_validate_kwargs: dict[str, Any] = {}

    def _check_authentication(self) -> None:
        return None

    def _validate_required_params(self, **kwargs: Any) -> None:
        self.last_validate_kwargs = dict(kwargs)
        for k, v in kwargs.items():
            # Required params must be present (non-empty for strings, not None).
            assert v is not None, f"{k} is None"
            if isinstance(v, str):
                assert v != "", f"{k} is empty string"

    def _make_request(self, method: str, endpoint: str, payload: dict) -> dict:
        self.calls.append((method, endpoint, payload))
        return self._responses.pop(0)


def _valid_create_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "demo",
        "logic_compute_group_id": "lcg-1",
        "project_id": "project-1",
        "workspace_id": "ws-1",
        "image": "reg/img:latest",
        "image_type": "SOURCE_PRIVATE",
        "command": "bash serve.sh",
        "model_id": "model-1",
        "model_version": 1,
        "port": 8000,
        "replicas": 2,
        "node_num_per_replica": 1,
        "spec_id": "spec-1",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Happy-path payload shape
# ---------------------------------------------------------------------------


def test_create_builds_payload_with_required_numeric_fields_coerced() -> None:
    api = _DummyAPI(responses=[{"code": 0, "data": {"inference_serving_id": "sv-1"}}])

    result = create_inference_serving(api, **_valid_create_kwargs())

    assert result["code"] == 0
    method, endpoint, payload = api.calls[0]
    assert method == "POST"
    assert endpoint == "/openapi/v1/inference_servings/create"

    # Required string fields pass through.
    assert payload["name"] == "demo"
    assert payload["logic_compute_group_id"] == "lcg-1"
    assert payload["workspace_id"] == "ws-1"
    assert payload["spec_id"] == "spec-1"
    # Numeric fields coerced to int.
    assert payload["model_version"] == 1
    assert payload["port"] == 8000
    assert payload["replicas"] == 2
    assert payload["node_num_per_replica"] == 1
    assert payload["task_priority"] == 10  # default
    # custom_domain omitted when not supplied.
    assert "custom_domain" not in payload


def test_create_includes_custom_domain_when_supplied() -> None:
    api = _DummyAPI(responses=[{"code": 0, "data": {"inference_serving_id": "sv-1"}}])
    create_inference_serving(
        api, **_valid_create_kwargs(custom_domain="my-serving.example.com")
    )
    payload = api.calls[0][2]
    assert payload["custom_domain"] == "my-serving.example.com"


def test_create_validates_all_required_params_including_numerics() -> None:
    """Codex flagged missing numeric validation — confirm it's now in place."""
    api = _DummyAPI(responses=[{"code": 0, "data": {}}])
    create_inference_serving(api, **_valid_create_kwargs())

    required = {
        "name",
        "logic_compute_group_id",
        "project_id",
        "workspace_id",
        "image",
        "image_type",
        "command",
        "model_id",
        "spec_id",
        "model_version",
        "port",
        "replicas",
        "node_num_per_replica",
    }
    assert required.issubset(api.last_validate_kwargs.keys())


# ---------------------------------------------------------------------------
# _coerce_int: bool + float edge cases (Codex round-2 finding)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,bad_value,match",
    [
        ("replicas", True, "got bool True"),
        ("port", False, "got bool False"),
        ("model_version", 1.7, "got float 1.7"),
        ("node_num_per_replica", "not-a-number", "got 'not-a-number'"),
    ],
)
def test_create_rejects_bool_and_nonwhole_float_numerics(
    field: str, bad_value: Any, match: str
) -> None:
    api = _DummyAPI(responses=[{"code": 0, "data": {}}])
    kwargs = _valid_create_kwargs(**{field: bad_value})
    with pytest.raises(InspireAPIError, match=match):
        create_inference_serving(api, **kwargs)
    # No HTTP request must have been made — validation caught it first.
    assert api.calls == []


def test_create_accepts_string_of_int_and_whole_float() -> None:
    api = _DummyAPI(responses=[{"code": 0, "data": {}}])
    create_inference_serving(
        api, **_valid_create_kwargs(model_version="3", port=8000.0, replicas=4)
    )
    payload = api.calls[0][2]
    assert payload["model_version"] == 3
    assert payload["port"] == 8000
    assert payload["replicas"] == 4


# ---------------------------------------------------------------------------
# Error propagation (code != 0)
# ---------------------------------------------------------------------------


def test_create_raises_on_nonzero_code() -> None:
    api = _DummyAPI(
        responses=[{"code": 100002, "message": "参数错误"}]
    )
    with pytest.raises(InspireAPIError, match="Failed to create inference serving"):
        create_inference_serving(api, **_valid_create_kwargs())


def test_detail_happy_and_error_paths() -> None:
    api = _DummyAPI(
        responses=[
            {"code": 0, "data": {"inference_serving_id": "sv-1", "status": "RUNNING"}},
            {"code": 100002, "message": "参数错误"},
        ]
    )

    result = get_inference_serving_detail(api, "sv-1")
    assert result["code"] == 0
    assert result["data"]["inference_serving_id"] == "sv-1"
    method, endpoint, payload = api.calls[0]
    assert method == "POST"
    assert endpoint == "/openapi/v1/inference_servings/detail"
    assert payload == {"inference_serving_id": "sv-1"}

    with pytest.raises(InspireAPIError, match="Failed to get inference serving detail"):
        get_inference_serving_detail(api, "sv-bad")


def test_stop_happy_and_error_paths() -> None:
    api = _DummyAPI(
        responses=[
            {"code": 0, "message": "success"},
            {"code": 100002, "message": "参数错误"},
        ]
    )

    assert stop_inference_serving(api, "sv-1") is True
    method, endpoint, payload = api.calls[0]
    assert method == "POST"
    assert endpoint == "/openapi/v1/inference_servings/stop"
    assert payload == {"inference_serving_id": "sv-1"}

    with pytest.raises(InspireAPIError, match="Failed to stop inference serving"):
        stop_inference_serving(api, "sv-bad")
