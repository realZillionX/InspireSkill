"""A platform that did not answer must never read as an answer.

Every workspace-wide question the CLI asks is a fan-out of one request per
compute group, which is exactly the shape a rate limiter reacts to. These
tests pin the boundary: a ``429`` is transport state, never data.
"""

from __future__ import annotations

import time

import pytest

from inspire.cli.utils.id_resolver import is_stale_handle_error
from inspire.platform.web import session as ws
from inspire.platform.web.browser_api import core as browser_core
from inspire.platform.web.browser_api.availability import api as availability_api
from inspire.platform.web.session import TransientAPIError, WebSession
from inspire.platform.web.session.models import is_transient_api_error
from inspire.platform.web.session.retry import (
    MAX_ATTEMPTS,
    backoff_delay,
    retry_after_seconds,
    with_transient_retry,
)


class _Response:
    def __init__(self, status_code: int, *, headers=None, payload=None) -> None:  # noqa: ANN001
        self.status_code = status_code
        self.headers = headers or {}
        self.text = "Too Many Requests"
        self._payload = payload or {"ok": True}

    def json(self):  # noqa: ANN201
        return self._payload


class _HTTP:
    """A requests-like session that answers a scripted sequence of responses."""

    def __init__(self, *responses: _Response) -> None:
        self._responses = list(responses)
        self.calls = 0

    def _next(self) -> _Response:
        self.calls += 1
        return self._responses[min(self.calls - 1, len(self._responses) - 1)]

    def get(self, url, headers=None, timeout=None):  # noqa: ANN001, ANN201
        return self._next()

    def post(self, url, headers=None, json=None, timeout=None):  # noqa: ANN001, ANN201
        return self._next()

    def close(self) -> None:
        pass


def _session() -> WebSession:
    return WebSession(
        storage_state={"cookies": [{"name": "session", "value": "abc"}]},
        cookies={"session": "abc"},
        workspace_id="ws-test",
        created_at=0,
    )


def _install(monkeypatch, http: _HTTP) -> list[float]:  # noqa: ANN001
    """Point ``request_json`` at *http* and record what it would have slept."""
    slept: list[float] = []
    monkeypatch.setattr(ws, "build_requests_session", lambda _session, _url: http)
    monkeypatch.setattr(ws, "_BROWSER_API_FORCE_BROWSER", False)
    monkeypatch.setattr(
        "inspire.platform.web.session.retry.time.sleep",
        slept.append,
    )
    return slept


# ---------------------------------------------------------------------------
# Transport classification
# ---------------------------------------------------------------------------


def test_rate_limited_response_is_typed_as_transient(monkeypatch) -> None:  # noqa: ANN001
    http = _HTTP(_Response(429, headers={"Retry-After": "0"}))
    _install(monkeypatch, http)

    with pytest.raises(TransientAPIError) as excinfo:
        ws.request_json(_session(), "GET", "https://example.test")

    assert excinfo.value.status == 429
    assert is_transient_api_error(excinfo.value)


def test_client_error_stays_an_ordinary_api_error(monkeypatch) -> None:  # noqa: ANN001
    http = _HTTP(_Response(400))
    _install(monkeypatch, http)

    with pytest.raises(ValueError) as excinfo:
        ws.request_json(_session(), "GET", "https://example.test")

    assert not isinstance(excinfo.value, TransientAPIError)
    assert http.calls == 1  # a bad request is not worth repeating


def test_a_burst_of_rate_limiting_is_waited_out(monkeypatch) -> None:  # noqa: ANN001
    http = _HTTP(
        _Response(429, headers={"Retry-After": "0"}),
        _Response(429, headers={"Retry-After": "0"}),
        _Response(200, payload={"data": [1, 2, 3]}),
    )
    slept = _install(monkeypatch, http)

    assert ws.request_json(_session(), "GET", "https://example.test") == {
        "data": [1, 2, 3]
    }
    assert http.calls == 3
    assert slept == [0.0, 0.0]


def test_sustained_rate_limiting_surfaces_rather_than_looping(monkeypatch) -> None:  # noqa: ANN001
    http = _HTTP(_Response(429, headers={"Retry-After": "0"}))
    _install(monkeypatch, http)

    with pytest.raises(TransientAPIError):
        ws.request_json(_session(), "GET", "https://example.test")

    assert http.calls == MAX_ATTEMPTS


def test_v2_envelope_throttling_is_transient_despite_http_200() -> None:
    with pytest.raises(TransientAPIError):
        browser_core._v2_result(
            {
                "ResponseMetadata": {
                    "Error": {"Code": "Throttling", "Message": "slow down"}
                }
            }
        )


def test_v2_envelope_business_error_is_not_transient() -> None:
    with pytest.raises(ValueError) as excinfo:
        browser_core._v2_result(
            {
                "ResponseMetadata": {
                    "Error": {"Code": "AccessForbidden", "Message": "nope"}
                }
            }
        )

    assert not isinstance(excinfo.value, TransientAPIError)


# ---------------------------------------------------------------------------
# Retry arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ({"Retry-After": "12"}, 12.0),
        ({"retry-after": "0"}, 0.0),
        ({"Retry-After": "not-a-number"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_retry_after_reads_the_delta_seconds_form(header, expected) -> None:  # noqa: ANN001
    assert retry_after_seconds(header) == expected


def test_retry_after_reads_the_http_date_form() -> None:
    future = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 30))

    delay = retry_after_seconds({"Retry-After": future})

    assert delay is not None
    assert 20 <= delay <= 31


def test_backoff_prefers_a_reasonable_retry_after() -> None:
    error = TransientAPIError("429", status=429, retry_after=2.0)

    assert backoff_delay(0, error) == 2.0


def test_backoff_ignores_an_unreasonably_long_retry_after() -> None:
    error = TransientAPIError("429", status=429, retry_after=3600.0)

    # Falls back to the local schedule rather than parking the command for an
    # hour; the caller gets an error it can act on instead.
    assert backoff_delay(0, error) <= 1.0


def test_with_transient_retry_reraises_the_last_failure() -> None:
    attempts = {"n": 0}

    def _always_limited() -> None:
        attempts["n"] += 1
        raise TransientAPIError("429", status=429, retry_after=0)

    with pytest.raises(TransientAPIError):
        with_transient_retry(_always_limited, max_attempts=4, sleep=lambda _s: None)

    assert attempts["n"] == 4


def test_with_transient_retry_leaves_other_errors_alone() -> None:
    attempts = {"n": 0}

    def _broken() -> None:
        attempts["n"] += 1
        raise ValueError("API returned 400: bad request")

    with pytest.raises(ValueError):
        with_transient_retry(_broken, sleep=lambda _s: None)

    assert attempts["n"] == 1


# ---------------------------------------------------------------------------
# Downstream verdicts
# ---------------------------------------------------------------------------


def test_rate_limiting_is_never_a_stale_handle() -> None:
    """Tombstoning a cached name on a 429 would delete a live resource."""
    assert not is_stale_handle_error(TransientAPIError("429", status=429))
    assert not is_stale_handle_error(ValueError("API returned 429: Too Many Requests"))
    assert is_stale_handle_error(ValueError("API returned 404: not found"))


def test_availability_refuses_to_report_a_rate_limited_group(monkeypatch) -> None:  # noqa: ANN001
    """Skipping the group would under-report capacity as if it were measured."""
    monkeypatch.setattr(
        availability_api,
        "_list_live_compute_groups",
        lambda **_kwargs: [{"logic_compute_group_id": "lcg-a", "name": "H200"}],
    )
    monkeypatch.setattr(
        availability_api,
        "_request_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TransientAPIError("API returned 429: Too Many Requests", status=429)
        ),
    )

    class _Session:
        all_workspace_ids = ["workspace-one"]
        all_workspace_names = {"workspace-one": "Training"}
        workspace_id = "workspace-one"

    with pytest.raises(TransientAPIError):
        availability_api.get_accurate_resource_availability(
            workspace_id="workspace-one",
            session=_Session(),  # type: ignore[arg-type]
        )


def test_availability_refuses_to_report_zero_free_nodes_on_a_rate_limit(
    monkeypatch,  # noqa: ANN001
) -> None:
    monkeypatch.setattr(
        availability_api,
        "_list_live_compute_groups",
        lambda **_kwargs: [{"logic_compute_group_id": "lcg-a", "name": "H200"}],
    )
    monkeypatch.setattr(
        availability_api,
        "_request_json",
        lambda *_args, **_kwargs: {
            "Result": {
                "logic_resouces": {"gpu_total": 8, "gpu_used": 0},
                "gpu_type_stats": [{"gpu_info": {"gpu_type_display": "H200"}}],
            }
        },
    )
    monkeypatch.setattr(
        availability_api,
        "list_node_dimension",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TransientAPIError("API returned 429: Too Many Requests", status=429)
        ),
    )

    class _Session:
        all_workspace_ids = ["workspace-one"]
        all_workspace_names = {"workspace-one": "Training"}
        workspace_id = "workspace-one"

    with pytest.raises(TransientAPIError):
        availability_api.get_accurate_resource_availability(
            workspace_id="workspace-one",
            session=_Session(),  # type: ignore[arg-type]
        )
