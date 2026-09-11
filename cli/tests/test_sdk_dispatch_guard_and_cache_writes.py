"""Dispatch, login-guard and cache-write regressions, with isolated accounts and fake transports."""

from __future__ import annotations

import asyncio
from copy import copy
import sqlite3
import time
import traceback
from types import SimpleNamespace

import requests
import httpx
import pytest

from test_sdk import client as client
from test_sdk_async import tracked as tracked
from inspire.sdk import (
    AuthenticationError,
    AuthenticationCooldownError,
    InspireAsyncClient,
    InspireError,
    ImageRef,
    JobRef,
    NotebookRef,
    WorkspaceRef,
    Resource,
    Job,
    AmbiguousResourceError,
    MutationUncertainError,
    SubmissionUncertainError,
    TransportError,
    WaitTimeoutError,
)
from inspire.platform.web.session import auth
from inspire.platform.web.session.models import WebSession, TransientAPIError
from inspire.platform.web.session.envelope import _v2_result


@pytest.fixture
def refused(client, monkeypatch):
    logins = []

    def login(*args, **kwargs):
        session = copy(client._transport._session)
        session.created_at += len(logins) + 1
        logins.append(session)
        return session

    monkeypatch.setattr(auth, "login_without_browser", login)
    monkeypatch.setattr(auth, "get_credentials", lambda account: ("test", "unused"))
    monkeypatch.setattr(auth, "renew_web_session_without_credentials", lambda session: None)
    monkeypatch.setattr(auth, "_persist", lambda *args, **kwargs: None)
    monkeypatch.setattr(WebSession, "load", staticmethod(lambda **kwargs: None))

    async def send(self, request, **kwargs):
        return httpx.Response(401, json={}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    http = requests.Session()
    monkeypatch.setattr(
        http,
        "request",
        lambda *a, **kw: SimpleNamespace(status_code=401, text="", headers={}, json=dict),
    )
    monkeypatch.setattr(client._transport, "_http", http)
    from inspire.sdk.resources import Workspaces

    monkeypatch.setattr(
        Workspaces, "_all", lambda self: self.client._transport.request("GET", "/fake")
    )
    return logins


def test_async_refused_login_is_not_rebuilt_per_operation(tracked, refused):
    async def run():
        async with InspireAsyncClient("alpha") as sdk:
            for _ in range(5):
                with pytest.raises(InspireError):
                    await sdk.workspaces.list()

    asyncio.run(run())
    assert len(refused) == 1


def test_guard_refusal_is_authentication_error(client, refused):
    for _ in range(3):
        with pytest.raises(AuthenticationError):
            client.workspaces.list()
    assert len(refused) == 1


@pytest.mark.parametrize("create", [True, False])
@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (500, None, False),
        (200, "InternalError", False),
        (200, "InternalFailure", False),
        (200, "InternalServerError", False),
        (200, "RequestTimeout", False),
        (200, "ServiceUnavailable", False),
        (429, None, True),
        (200, "Throttling", True),
    ],
)
def test_write_outcome_matches_http_and_envelope(
    client, monkeypatch, create, status, code, retryable
):
    sent = []

    def request(*args, **kwargs):
        sent.append(1)
        return SimpleNamespace(
            status_code=status,
            json=lambda: {
                "ResponseMetadata": {"Error": {"Code": code, "Message": "backend fault"}}
            },
        )

    http = requests.Session()
    monkeypatch.setattr(http, "request", request)
    monkeypatch.setattr(client._transport, "_http", http)
    expected = TransportError if retryable else (
        SubmissionUncertainError if create else MutationUncertainError
    )
    with pytest.raises(expected) as caught:
        with client._transport.single_send("review-write", create=create):
            _v2_result(client._transport.request("POST", "/fake"))
    assert caught.value.retryable is retryable
    cause = caught.value.__cause__
    assert isinstance(cause, TransientAPIError)
    assert cause.status == (None if status == 200 else status)
    assert cause.code == code
    assert sent == [1]


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("mutation", ["delete", "register"])
def test_cache_failure_preserves_image_mutation(client, monkeypatch, phase, mutation):
    from inspire.platform.web import browser_api

    calls = []
    fences = []

    def invalidate():
        fences.append(1)
        if phase == "before" or len(fences) == 2:
            raise (
                sqlite3.OperationalError("readonly") if phase == "before" else OSError("disk full")
            )

    monkeypatch.setattr(client.images, "_invalidate_images", invalidate)
    monkeypatch.setattr(browser_api, "delete_image", lambda **kw: calls.append("delete"))

    def create(**kwargs):
        calls.append("register")
        return {"image_id": "image-1", "address": "registry/fake"}

    monkeypatch.setattr(browser_api, "create_image", create)
    ws = WorkspaceRef("ws", client.account, client.base_url, "ws-test", "ws-test")
    monkeypatch.setattr(client.workspaces, "get", lambda *a, **kw: Resource("ws", ws))
    if mutation == "delete":
        assert (
            client.images.delete(
                ImageRef("img:v1", client.account, client.base_url, "image-1", "ws-test")
            )
            is None
        )
    else:
        handle = client.images.register("img", workspace=ws)
        assert handle.ref.key == "image-1"
    assert calls == [mutation]
    assert len(fences) == 2
    assert client.cache.ttl == 0
    assert client.cache.stats()["entries"] == 0


def test_sdk_resolution_preserves_cli_pick_order_and_columns(client, monkeypatch, tmp_path):
    from inspire.sdk.identity_cache import IdentityCache
    from inspire.services.catalog.resource_index import (
        ResourceIndex,
        ResourceIdentity,
        scope_for_session,
    )

    path = tmp_path / "identities.sqlite3"
    monkeypatch.setattr(
        "inspire.services.catalog.resource_index.resource_index_path", lambda account=None: path
    )
    index = ResourceIndex(path)
    scope = scope_for_session(
        client._transport._session,
        resource_type="notebook",
        workspace_id="ws-test",
        owner_scope="self",
    )
    index.replace_name(
        scope,
        "dup",
        [
            ResourceIdentity(
                "aaa-old",
                "dup",
                owner_id="user",
                status="STOPPED",
                created_at="2020-01-01",
                compute_group="cpu",
            ),
            ResourceIdentity(
                "zzz-new",
                "dup",
                owner_id="user",
                status="RUNNING",
                created_at="2026-09-01",
                compute_group="gpu",
            ),
        ],
    )

    def cli_candidates():
        return [
            (r.resource_id, r.owner_id, r.status, r.created_at, r.compute_group)
            for r in index.lookup(scope, "dup")
        ]

    from inspire.cli.commands.notebook.notebook_lookup import _resolve_notebook_target

    def cli_pick():
        return _resolve_notebook_target(
            None,
            session=client._transport._session,
            base_url=client.base_url,
            identifier="dup",
            json_output=True,
            workspace_ids=["ws-test"],
            pick=1,
            cache_index=index,
        )

    before = cli_candidates()
    assert cli_pick() == ("zzz-new", "ws-test", "gpu")
    assert before[0][0] == "zzz-new"
    client.cache._identity = IdentityCache(client.account, 60, client.base_url)
    rows = [
        Resource("dup", NotebookRef("dup", client.account, client.base_url, key, "ws-test"))
        for key in ("aaa-old", "zzz-new")
    ]
    for _ in range(3):
        with pytest.raises(AmbiguousResourceError):
            client.notebooks._indexed_resolution("dup", NotebookRef, "ws-test", lambda: rows)
        assert cli_candidates() == before
        assert cli_pick() == ("zzz-new", "ws-test", "gpu")


def test_logs_single_instance_is_one_pod(client, monkeypatch):
    from inspire.services.job import job_logs

    ref = JobRef("job", client.account, client.base_url, "job-1", "ws-test")
    monkeypatch.setattr(client.jobs, "get", lambda *a, **kw: Job("job", ref, "RUNNING", "RUNNING"))
    captured = []

    def fetch(**kwargs):
        captured.append(kwargs["pod_names"])
        return [], 0

    monkeypatch.setattr(job_logs, "fetch_job_logs", fetch)
    monkeypatch.setattr("inspire.services.job.job_events.list_all_job_instances",
                        lambda *a, **kw: [{"name": "worker-0"}])
    client.jobs.logs(ref, instance="worker-0")
    assert captured == [["worker-0"]]


def test_cooldown_keeps_actionable_login_guard_message(client, refused, monkeypatch):
    message = "Account alpha blocked until 12:00; run inspire account set --password."
    blocked = auth.AuthenticationError(message)
    blocked.retry_at = time.time() + 300

    def login(*args, **kwargs):
        raise blocked

    monkeypatch.setattr(auth, "login_without_browser", login)
    with pytest.raises(AuthenticationCooldownError) as caught:
        client.workspaces.list()
    assert str(caught.value) == message
    assert caught.value.retry_at == blocked.retry_at


@pytest.mark.parametrize(
    "error",
    [
        AuthenticationError("secret-403"),
        TransportError("secret-throttle"),
        TransientAPIError("secret-backend", status=503, retry_after=7),
    ],
)
def test_api_key_errors_keep_type_and_metadata_without_secret(client, monkeypatch, error):
    from inspire.platform.web.browser_api import api_keys

    def request(*args, **kwargs):
        raise error

    monkeypatch.setattr(api_keys, "_request_json", request)
    with pytest.raises(type(error)) as caught:
        api_keys._call("GetMyAPIList", {}, client._transport._session)
    assert getattr(caught.value, "status", None) == getattr(error, "status", None)
    assert getattr(caught.value, "retry_after", None) == getattr(error, "retry_after", None)
    assert "secret-" not in "".join(traceback.format_exception(caught.value))


def test_deadline_does_not_claim_remote_state_unchanged(client):
    client._transport.deadline = time.monotonic() - 1
    with pytest.raises(WaitTimeoutError) as caught:
        client._transport.check_deadline()
    assert "does not stop remote workloads" in str(caught.value)
    assert "unchanged" not in str(caught.value)


@pytest.mark.parametrize(
    "code,expected", [("Throttling", TransportError), ("InternalError", TransportError)]
)
def test_api_key_read_envelope_keeps_retryable_sdk_error(client, monkeypatch, code, expected):
    from inspire.platform.web.browser_api import api_keys

    monkeypatch.setattr(
        api_keys,
        "_request_json",
        lambda *a, **kw: {"ResponseMetadata": {"Error": {"Code": code, "Message": "secret-value"}}},
    )
    with pytest.raises(expected) as caught:
        client.api_keys.list()
    assert caught.value.retryable
    assert "secret-value" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    "facade", ["jobs", "notebooks", "hpc", "ray", "servings", "tensorboards", "models"]
)
def test_sequence_only_status_rejects_bare_string_before_io(client, facade):
    from inspire.sdk import ValidationError

    with pytest.raises(ValidationError, match="refs must be a sequence, not a string"):
        getattr(client, facade).status("workload")


def test_sequence_only_dataset_specs_reject_bare_string_before_io(client):
    from inspire.sdk import ValidationError

    with pytest.raises(ValidationError, match="specs must be a sequence, not a string"):
        client.datasets.validate("dataset:1", workspace="ws")


def test_operation_views_share_generation_evidence_but_not_write_claims(client):
    from inspire.sdk._async_runtime import AsyncRuntime

    runtime = AsyncRuntime({}, None)
    runtime._client = client
    first, second = runtime._operation_client(), runtime._operation_client()
    first._transport._decisions.rebuilt(100)
    first._transport._write = {"sent": False}
    assert second._transport._unproven_rebuild == 100
    assert second._transport._write is None
    second._transport._decisions.success(99, 200, False)
    assert first._transport._unproven_rebuild == 100
    second._transport._decisions.success(100, 201, False)
    assert first._transport._unproven_rebuild is None
    assert client._transport._last_success == 201


@pytest.mark.parametrize("facade", ["ray", "servings"])
def test_async_bulk_status_rejects_bare_string_before_io(tracked, facade):
    from inspire.sdk import ValidationError

    async def run():
        async with InspireAsyncClient("alpha") as sdk:
            with pytest.raises(ValidationError, match="refs must be a sequence, not a string"):
                await getattr(sdk, facade).status("workload")

    asyncio.run(run())


def test_real_cache_wiring_degrades_when_existing_index_is_unwritable(
    client, monkeypatch, tmp_path
):
    from inspire.platform.web import browser_api

    path = tmp_path / "existing-index.sqlite3"
    path.touch()
    monkeypatch.setattr(
        "inspire.services.catalog.resource_index.resource_index_path", lambda account=None: path
    )

    def unwritable():
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(client.cache._identity_invalidation, "invalidate_images", unwritable)
    key = ("images", client.account, client.base_url, "ws-test", "private")
    client.cache._entries[key] = (time.monotonic() + 60, ["stale"])
    sent = []
    monkeypatch.setattr(
        browser_api, "delete_image", lambda **kwargs: sent.append(kwargs["image_id"])
    )
    ref = ImageRef("img:v1", client.account, client.base_url, "image-1", "ws-test")
    assert client.images.delete(ref) is None
    assert sent == ["image-1"]
    assert client.cache.stats()["entries"] == 0
    assert client.cache._get(key, lambda: ["live"]) == ["live"]
    assert client.cache.stats()["entries"] == 0


def test_cache_failure_preserves_original_mutation_error(client, monkeypatch):
    from inspire.platform.web import browser_api

    def invalidation():
        raise OSError("disk full")

    monkeypatch.setattr(client.images, "_invalidate_images", invalidation)
    original = MutationUncertainError("Mutation may have succeeded")

    def delete(**kwargs):
        raise original

    monkeypatch.setattr(browser_api, "delete_image", delete)
    ref = ImageRef("img:v1", client.account, client.base_url, "image-1", "ws-test")
    with pytest.raises(MutationUncertainError) as caught:
        client.images.delete(ref)
    assert caught.value is original


@pytest.mark.parametrize(
    "code",
    [
        "SlowDown",
        "Throttling",
        "ThrottlingException",
        "TooManyRequests",
        "TooManyRequestsException",
        "  THROTTLING_EXCEPTION  ",
        "too_many_requests",
    ],
)
def test_throttling_envelope_remains_safe_to_retry(client, monkeypatch, code):
    sent = []

    def request(*args, **kwargs):
        sent.append(1)
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"ResponseMetadata": {"Error": {"Code": code, "Message": "wait"}}},
        )

    http = requests.Session()
    monkeypatch.setattr(http, "request", request)
    monkeypatch.setattr(client._transport, "_http", http)
    with pytest.raises(TransportError) as caught:
        with client._transport.single_send(create=True):
            _v2_result(client._transport.request("POST", "/fake"))
    assert caught.value.retryable
    assert sent == [1]


@pytest.mark.parametrize("code,retryable", [("Throttling", True), ("InternalError", False)])
def test_envelope_classification_survives_message_replacement(code, retryable):
    from inspire.platform.web.transport_core import _classify_after_dispatch

    with pytest.raises(TransientAPIError) as caught:
        _v2_result({"ResponseMetadata": {"Error": {"Code": code, "Message": "failure"}}})
    error = caught.value
    assert error.status is None
    assert error.code == code
    # API key operations replace the entire message to suppress sensitive text.
    error.args = ("API key operation failed; no secret was displayed.",)
    verdict = _classify_after_dispatch(error)
    if retryable:
        assert isinstance(verdict, TransportError)
        assert verdict.retryable
    else:
        assert verdict is None
