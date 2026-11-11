"""Sanitized response fixtures shared by the blocking and native async SDK."""

import asyncio
from contextlib import nullcontext
import json

import httpx
import pytest
import requests

from test_sdk import client as client
from test_sdk_async import tracked as tracked
from inspire.platform.errors import _error_metadata
from inspire.platform.web.session.envelope import _v2_result
from inspire.platform.web.session.models import TransientAPIError
from inspire.platform.web.transport_core import _classify_after_dispatch
from inspire.sdk import (
    AuthenticationError, InspireAsyncClient, InspireError, MutationUncertainError,
    SubmissionUncertainError, TransportError, ValidationError,
)
from inspire.sdk.resources import Workspaces


@pytest.fixture(params=[
    (200, "InvalidParameter", ValidationError),
    (400, "InvalidParameter", ValidationError),
    (403, "AccessDenied", AuthenticationError),
    (429, "Throttling", TransportError),
    (200, "Throttling", TransportError),
    (503, "InternalError", SubmissionUncertainError),
    (200, "InternalError", SubmissionUncertainError),
    (401, "Expired", SubmissionUncertainError),
])
def response_case(request):
    status, code, error_type = request.param
    return status, {
        "ResponseMetadata": {
            "Service": "hpc", "Action": "CreateJobConsole", "RequestId": "example-request",
            "Error": {"Code": code, "Message": 'unknown field "example_field"'},
        },
    }, error_type


@pytest.mark.parametrize("mode", ["create", "mutation", "read"])
def test_sdk_response_metadata_parity(client, tracked, monkeypatch, response_case, mode, caplog):
    status, payload, expected_type = response_case
    if mode == "read" and status == 401:
        pytest.skip("Authentication renewal is covered separately")
    if expected_type is SubmissionUncertainError:
        expected_type = {
            "create": SubmissionUncertainError, "mutation": MutationUncertainError,
            "read": TransportError,
        }[mode]
    sent = []
    private = "example-private-request-value"

    def response():
        sent.append(1)
        return httpx.Response(status, json=payload, headers={"Retry-After": "2"})

    monkeypatch.setattr(requests.Session, "request", lambda *a, **kw: response())

    async def send(*args, **kwargs):
        return response()

    monkeypatch.setattr(httpx.AsyncClient, "send", send)

    def load(self):
        transport = self.client._transport
        scope = nullcontext() if mode == "read" else transport.single_send(
            "example-operation", create=mode == "create", inspect="HPC jobs",
        )
        with scope:
            return _v2_result(transport.request("POST", "/fake", body={"private": private}))

    monkeypatch.setattr(Workspaces, "_all", load)
    with pytest.raises(expected_type) as sync:
        client.workspaces.list()
    sync_count = len(sent)

    async def run():
        async with InspireAsyncClient("alpha") as sdk:
            with pytest.raises(expected_type) as caught:
                await sdk.workspaces.list()
            return caught.value

    async_error = asyncio.run(run())
    expected = {
        "status": status, "code": payload["ResponseMetadata"]["Error"]["Code"],
        "service": "hpc", "action": "CreateJobConsole", "request_id": "example-request",
        "retry_after": 2.0,
    }
    assert _error_metadata(sync.value) == _error_metadata(async_error) == expected
    assert str(sync.value) == str(async_error)
    assert len(sent) == 2 * sync_count
    if mode != "read":
        assert sync_count == 1
    if mode == "create" and expected_type is SubmissionUncertainError:
        assert sync.value.operation_id == async_error.operation_id == "example-operation"
        assert sync.value.inspect == async_error.inspect == "HPC jobs"
        assert sync.value.retryable is False
    assert private not in str(sync.value) + repr(vars(sync.value)) + caplog.text


def test_synthetic_envelope_and_missing_metadata():
    payload = {"ResponseMetadata": {
        "Service": "hpc", "Action": "CreateJobConsole", "RequestId": "example-request",
        "Error": {"Code": "InvalidParameter", "Message": 'unknown field "example_field"'},
    }}
    with pytest.raises(ValueError) as caught:
        _v2_result(payload)
    error = _classify_after_dispatch(caught.value)
    assert isinstance(error, ValidationError)
    assert isinstance(error, ValueError)
    assert str(error) == 'API error: InvalidParameter: unknown field "example_field"'
    assert _error_metadata(error) == dict(
        code="InvalidParameter", status=None, service="hpc", action="CreateJobConsole",
        request_id="example-request", retry_after=None,
    )
    with pytest.raises(ValueError) as missing:
        _v2_result({"ResponseMetadata": {"Error": {}}})
    assert str(missing.value) == "API error: Error: unknown error"
    assert all(value is None for value in _error_metadata(missing.value).values())


def test_constructor_compatibility():
    assert str(InspireError("first", "second")) == "('first', 'second')"
    assert str(InspireError()) == ""
    error = TransientAPIError(message="unchanged", code="Throttling", status=429, retry_after=3)
    assert isinstance(error, ValueError)
    assert str(error) == "unchanged"
    assert error.service is error.action is error.request_id is None
    assert json.dumps(_error_metadata(error))


def test_wrapped_business_error_keeps_context(client, monkeypatch):
    def load(self):
        try:
            _v2_result({"ResponseMetadata": {
                "RequestId": "example-wrapped", "Error": {"Code": "Conflict"},
            }})
        except ValueError as cause:
            raise ValueError("Resource update failed") from cause

    monkeypatch.setattr(Workspaces, "_all", load)
    with pytest.raises(ValidationError, match="Resource update failed") as caught:
        client.workspaces.list()
    assert caught.value.code == "Conflict"
    assert caught.value.request_id == "example-wrapped"
    assert caught.value.status is None


@pytest.mark.parametrize("status", [400, 403, 429, 503])
def test_non_json_http_errors_have_only_observed_metadata(status):
    from inspire.platform.web.transport_policy import _SDK_POLICY

    with pytest.raises((InspireError, TransientAPIError)) as caught:
        _SDK_POLICY.response(httpx.Response(status, text="platform detail"))
    assert _error_metadata(caught.value) == dict(
        code=None, status=status, service=None, action=None, request_id=None, retry_after=None,
    )


def test_browser_error_retains_envelope_metadata():
    from inspire.platform.web.session.browser_client import _BrowserHTTPError
    from inspire.platform.web.transport_policy import _SDK_POLICY

    body = json.dumps({"ResponseMetadata": {
        "RequestId": "example-browser", "Error": {"Code": "InvalidParameter"},
    }})
    with pytest.raises(ValidationError) as caught:
        _SDK_POLICY.browser_error(_BrowserHTTPError(400, body))
    assert caught.value.code == "InvalidParameter"
    assert caught.value.request_id == "example-browser"
    assert caught.value.status == 400


def test_response_metadata_excludes_unrelated_data(caplog):
    from inspire.platform.web.transport_policy import _SDK_POLICY

    private = "example-unrelated-private-value"
    payload = {"ResponseMetadata": {
        "Error": {"Code": "InvalidParameter", "Message": "Invalid field", "body": private},
        "Authorization": private, "Cookie": private, "request_body": private,
    }, "secrets": private}
    envelope = _SDK_POLICY.response(httpx.Response(200, json=payload))
    with pytest.raises(ValueError) as caught:
        _v2_result(envelope)
    assert private not in str(caught.value) + repr(vars(caught.value)) + caplog.text
    assert set(vars(caught.value)) == set(_error_metadata(caught.value))
