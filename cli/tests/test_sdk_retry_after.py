"""Offline timing and error contracts through both real transport drivers."""

import asyncio
import json
from email.utils import formatdate

import httpx
import pytest
import requests

from inspire.platform.errors import (
    MutationUncertainError,
    SubmissionUncertainError,
    TransportError,
    WaitTimeoutError,
)
from inspire.platform.web import session as ws
from inspire.platform.web.session.retry import backoff_delay
from inspire.platform.web.transport import Transport
from inspire.platform.web.transport_core import Observation


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("header,delay", [
    ("120", 120.0), (formatdate(1120, usegmt=True), 120.0),
    (None, 0.5), ("invalid", 0.5), ("nan", 0.5), ("inf", 0.5), ("-1", 0.5),
    ("0", 0.0),
])
@pytest.mark.parametrize("mode", ["success", "exhausted", "deadline", "create", "stop"])
def test_sdk_retry_timing(monkeypatch, asynchronous, status, header, delay, mode):
    transport = Transport(None, "https://example.test", username="")
    transport.adopt_session(ws.WebSession(
        access_token="test", refresh_token="test", access_expires_at=9999999999,
        refresh_expires_at=9999999999, created_at=1, base_url="https://example.test",
    ))
    now, sends, sleeps = [0.0], [], []
    transport.deadline = 0.1 if mode == "deadline" else 1000
    if mode in {"create", "stop"}:
        transport._write = {
            "used": False, "sent": False, "create": mode == "create", "operation_id": "test",
        }
    monkeypatch.setattr(transport, "_observe", lambda: Observation(now[0], 1, 0.5))
    monkeypatch.setattr("inspire.platform.web.session.retry.time.time", lambda: 1000)

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    async def async_sleep(seconds):
        sleep(seconds)

    def reply():
        sends.append(now[0])
        return 200 if mode == "success" and len(sends) == 2 else status

    payload = {"ResponseMetadata": {
        "Service": "hpc", "Action": "GetJob", "RequestId": "example-retry-request",
        "Error": {"Code": "Throttling" if status == 429 else "InternalError"},
    }}

    def sync_send(*args, **kwargs):
        response = requests.Response()
        response.status_code = reply()
        response._content = json.dumps(payload if response.status_code != 200 else {}).encode()
        if header is not None:
            response.headers["Retry-After"] = header
        return response

    async def async_send(*args, **kwargs):
        response_status = reply()
        return httpx.Response(
            response_status, json=payload if response_status != 200 else {},
            headers={} if header is None else {"Retry-After": header},
        )

    monkeypatch.setattr("time.sleep", sleep)
    monkeypatch.setattr(asyncio, "sleep", async_sleep)
    monkeypatch.setattr(requests.Session, "send", sync_send)
    monkeypatch.setattr(httpx.AsyncClient, "send", async_send)

    def request():
        if asynchronous:
            return asyncio.run(transport.request_async("POST", "/test", body={}))
        return transport.request("POST", "/test", body={})

    try:
        if mode == "success":
            assert request() == {}
            assert sends == [0, delay]
            assert sleeps == [delay]
        else:
            expected = TransportError
            if mode == "deadline" and delay > 0:
                expected = WaitTimeoutError
            elif mode in {"create", "stop"} and status == 503:
                expected = SubmissionUncertainError if mode == "create" else MutationUncertainError
            with pytest.raises(expected) as caught:
                request()
            assert caught.value.code == payload["ResponseMetadata"]["Error"]["Code"]
            assert caught.value.service == "hpc"
            assert caught.value.action == "GetJob"
            assert caught.value.request_id == "example-retry-request"
            if isinstance(caught.value, (TransportError, WaitTimeoutError)):
                assert caught.value.status == status
                assert caught.value.retry_after == (delay if header in {"120", "0", formatdate(1120, usegmt=True)} else None)
            exhausted = mode == "exhausted" or (mode == "deadline" and delay == 0)
            assert len(sends) == (3 if exhausted else 1)
            assert len(sleeps) == (2 if exhausted else 0)
            if exhausted:
                assert sleeps[0] == delay
                assert sleeps[1] == (1.0 if delay == 0.5 else delay)
    finally:
        transport.close()


def test_fallback_jitter_is_bounded():
    error = ws.TransientAPIError("busy")
    assert backoff_delay(0, error, jitter=0) == 0.375
    assert backoff_delay(0, error, jitter=1) == 0.625
    assert backoff_delay(10, error, jitter=1) == 8
