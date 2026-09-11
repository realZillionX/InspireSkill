"""SDK behavior contracts; all account state is isolated and all network calls are fake."""

from __future__ import annotations

import ast
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import pytest
from inspire.sdk import (
    InspireClient,
    Quota,
    JobRef,
    Resource,
    WorkspaceRef,
    Job,
    ValidationError,
    AmbiguousResourceError,
    ClientClosedError,
    ClientThreadError,
    SubmissionUncertainError,
    TransportError,
    AuthenticationError,
    WaitTimeoutError,
)
from inspire.platform.web.transport import Transport
from inspire.platform.web.session.models import WebSession, SessionExpiredError, TransientAPIError




@pytest.fixture
def client(tmp_path, monkeypatch):
    def reject_http(*args, **kwargs):
        raise AssertionError("SDK tests must mock all HTTP requests.")

    monkeypatch.setattr("requests.sessions.Session.send", reject_http)
    monkeypatch.setattr("httpx.AsyncClient.send", reject_http)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".inspire"
    for name in ("alpha", "beta"):
        d = root / "accounts" / name
        d.mkdir(parents=True)
        (d / "config.toml").write_text('[auth]\nusername="test"\npassword="unused"\n')
    (root / "current").write_text("alpha")
    c = InspireClient()
    c._transport._session = WebSession(
        storage_state={"cookies": [{"name": "x", "value": "fake"}]},
        created_at=time.time(),
        workspace_id="ws-test",
        account="alpha",
        login_username="test",
        base_url=c.base_url,
    )
    # Most tests exercise dispatch on an already active transport.
    c._transport._last_success = time.monotonic()
    yield c
    c.close()


def test_imports_are_lazy():
    script = '''import sys, importlib, pkgutil
import inspire.sdk
for module in pkgutil.iter_modules(inspire.sdk.__path__):
    importlib.import_module("inspire.sdk." + module.name)
import inspire.services.notebook.notebooks
import inspire.services.notebook.notebook_status
import inspire.services.notebook.notebook_output
import inspire.services.catalog.workload_quota
import inspire.services.job.job_output
for kind in ("hpc", "ray"):
    for module in ("submission", "status", "instances", "logs", "events", "output"):
        importlib.import_module(f"inspire.services.{kind}.{kind}_{module}")
import inspire.services.ray.ray_scaling
for module in ("serving.serving_submission", "serving.serving_status", "serving.serving_instances", "serving.serving_events", "serving.serving_logs", "serving.serving_views", "serving.serving_access", "serving.serving_output", "serving.serving_api_metrics", "tensorboard.tensorboards", "tensorboard.tensorboard_data", "catalog.image_writes", "catalog.model_writes"):
    importlib.import_module("inspire.services." + module)
import inspire.services.catalog.image_resolution
import inspire.services.catalog.task_priority
assert not any(x.startswith(("playwright", "click")) for x in sys.modules)
'''
    subprocess.run([sys.executable, "-c", script], check=True)


def test_account_fixed_and_scope_restored(client):
    from inspire.accounts import set_current_account, current_account

    set_current_account("beta")
    with client._transport.scope():
        assert current_account() == "alpha"
    assert current_account() == "beta"
    assert client.account == "alpha"


def test_thread_and_process_guards(client, monkeypatch):
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(client._transport.check)
        with pytest.raises(ClientThreadError):
            future.result()
    monkeypatch.setattr("inspire.platform.web.transport.os.getpid", lambda: -1)
    with pytest.raises(ClientThreadError):
        client._transport.check()
    monkeypatch.undo()


def test_close_is_owned_and_idempotent(client):
    counts = []
    other = Transport("beta", client.base_url, username="test")
    client._transport._http = SimpleNamespace(close=lambda: counts.append("alpha"))
    other._http = SimpleNamespace(close=lambda: counts.append("beta"))
    client.close()
    client.close()
    assert counts == ["alpha"]
    with pytest.raises(ClientClosedError):
        client._transport.check()
    other.check()
    other.close()
    assert counts == ["alpha", "beta"]


@pytest.mark.parametrize(
    "error",
    [
        SessionExpiredError("expired"),
        TransientAPIError("busy", status=500),
        ValueError("bad JSON"),
        __import__("requests").exceptions.ReadTimeout("timeout"),
    ],
)
def test_create_never_replays(client, monkeypatch, error):
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        client._transport._write["sent"] = True
        raise error

    monkeypatch.setattr(client._transport, "_once", fail)
    monkeypatch.setattr(client._transport, "_refresh", lambda: pytest.fail("write refreshed"))
    client._transport.allow_browser = True
    with pytest.raises(SubmissionUncertainError) as caught:
        with client._transport.scope(), client._transport.single_send("diagnostic-id", create=True):
            client._transport.request("POST", "/api/v2/train?Action=CreateJobConsole", body={})
    assert len(calls) == 1
    assert caught.value.operation_id == "diagnostic-id"
    assert caught.value.retryable is False


def test_envelope_read_retries_but_create_does_not(client, monkeypatch):
    replies = iter(
        [
            {"ResponseMetadata": {"Error": {"Code": "Throttling", "Message": "fake"}}},
            {"Result": {"ok": True}},
        ]
    )
    monkeypatch.setattr(client._transport, "_once", lambda *a, **k: next(replies))
    monkeypatch.setattr("inspire.platform.web.transport.time.sleep", lambda _: None)
    with client._transport.scope():
        assert client._transport.request("POST", "/api/v2/train?Action=ListJobs")["Result"]["ok"]
    calls = []

    def busy(*a, **k):
        client._transport._write["sent"] = True
        calls.append(1)
        return {"ResponseMetadata": {"Error": {"Code": "InternalError", "Message": "fake"}}}

    monkeypatch.setattr(client._transport, "_once", busy)
    with pytest.raises(SubmissionUncertainError) as caught:
        with client._transport.single_send(create=True):
            from inspire.platform.web.session.envelope import _v2_result

            _v2_result(client._transport.request("POST", "/api/v2/train?Action=CreateJobConsole"))
    assert calls == [1]
    assert str(caught.value.__cause__) == "API error: InternalError: fake"
    assert not caught.value.retryable



def test_invalid_ref_and_cursor_rejected(client):
    ref = JobRef("job", "beta", client.base_url, "key", "ws-test")
    with pytest.raises(ValidationError):
        client.jobs.get(ref)
    first = client.jobs._page(list(range(25)), query=("query",))
    assert first.items == tuple(range(20))
    with pytest.raises(ValidationError):
        client.jobs._page(list(range(25)), cursor=first.next_cursor, query=("other",))
    ref = JobRef("job", "alpha", client.base_url, "key", "ws-test")
    assert JobRef.from_dict(ref.to_dict()) == ref
    assert "key" not in repr(ref)
    with pytest.raises(ValidationError):
        WorkspaceRef.from_dict(ref.to_dict())


def test_resolution_checks_beyond_first_page(client, monkeypatch):
    from inspire.platform.web.browser_api.jobs import JobInfo

    ws = Resource("ws", WorkspaceRef("ws", "alpha", client.base_url, "ws-test", "ws-test"))
    monkeypatch.setattr(client.workspaces, "get", lambda _: ws)
    template = dict(
        status="job_running",
        command="",
        created_at="",
        finished_at=None,
        created_by_name="",
        created_by_id="",
        project_id="",
        project_name="",
        compute_group_name="",
        gpu_type="",
        gpu_count=1,
        instance_count=1,
        priority=4,
        workspace_id="ws-test",
    )
    rows = [
        JobInfo(job_id=str(i), name="same" if i in (0, 100) else str(i), **template)
        for i in range(101)
    ]
    calls = []

    def listing(**kw):
        calls.append(kw["page_num"])
        offset = (kw["page_num"] - 1) * 100
        return rows[offset : offset + 100], len(rows)

    monkeypatch.setattr("inspire.platform.web.browser_api.jobs.list_jobs", listing)
    with pytest.raises(AmbiguousResourceError):
        client.jobs.get("same", workspace="ws")
    assert calls == [1, 2]


def test_images_do_not_choose_first_source(client, monkeypatch):
    ws = Resource("ws", WorkspaceRef("ws", "alpha", client.base_url, "ws-test", "ws-test"))
    monkeypatch.setattr(client.workspaces, "get", lambda _: ws)
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.list_images_by_source",
        lambda source, **kw: [
            SimpleNamespace(name="image", version="v1", image_id=source, url="r/" + source,
                            source="SOURCE_OFFICIAL" if source == "official" else "SOURCE_PUBLIC",
                            visibility="VISIBILITY_" + source.upper(), status="", framework="")
        ],
    )
    with pytest.raises(AmbiguousResourceError):
        client.images.get("image:v1", workspace="ws")


def test_wait_binds_ref_unknown_is_not_success(client, monkeypatch):
    ref = JobRef("job", "alpha", client.base_url, "key", "ws-test")
    states = iter(["UNKNOWN", "RUNNING", "SUCCEEDED"])
    calls = []

    def get(selector):
        calls.append(selector)
        status = next(states)
        return Job("job", ref, status, status)

    monkeypatch.setattr(client.jobs, "get", get)
    monkeypatch.setattr("inspire.sdk.jobs.time.sleep", lambda _: None)
    assert client.jobs.wait(ref).status == "SUCCEEDED"
    assert calls == [ref, ref, ref]


def test_wait_budget_does_not_stop(client, monkeypatch):
    ref = JobRef("job", "alpha", client.base_url, "key", "ws-test")
    monkeypatch.setattr(client.jobs, "get", lambda _: Job("job", ref, "RUNNING", "job_running"))
    monkeypatch.setattr(client.jobs, "stop", lambda *a: pytest.fail("stopped"))
    with pytest.raises(WaitTimeoutError):
        client.jobs.wait(ref, timeout=0.005, poll_interval=0.01)


def test_running_job_delete_leaves_decision_to_platform(client, monkeypatch):
    ref = JobRef("job", "alpha", client.base_url, "key", "ws-test")
    monkeypatch.setattr(client.jobs, "get", lambda *a, **k: pytest.fail("unexpected precheck"))
    calls = []
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.jobs.delete_job", lambda *a, **k: calls.append(a)
    )
    client.jobs.delete(ref)
    assert calls == [("key",)]


def test_log_window_and_global_budget(client, monkeypatch):
    ref = JobRef("job", "alpha", client.base_url, "key", "ws-test")
    job = Job(
        "job",
        ref,
        "SUCCEEDED",
        "job_succeeded",
        created_at="1700000000000",
        finished_at="1700003600000",
    )
    monkeypatch.setattr(client.jobs, "get", lambda *a, **k: job)
    monkeypatch.setattr("inspire.services.job.job_events.list_all_job_instances",
                        lambda *a, **kw: [{"name": "worker-0"}, {"name": "worker-1"}])
    captured = []

    def logs(**kwargs):
        captured.append(kwargs)
        return [{"timestamp_ms": "1700003600000", "pod_name": "worker-1", "message": "x" * 100}], 50

    monkeypatch.setattr("inspire.platform.web.browser_api.jobs.list_train_job_logs", logs)
    result = client.jobs.logs(ref, limit=1, max_chars=20)
    assert len(result.text) == 20 and result.truncated
    assert captured[0]["pod_names"] == ["worker-0", "worker-1"]
    assert captured[0]["end_timestamp_ms"] == 1700004200000
    assert client.jobs.logs(ref, tail=1).total == 50


def test_quota_validation():
    with pytest.raises(ValidationError):
        Quota(True, 20, 200)
    with pytest.raises(ValidationError):
        Quota(1, 0, 200)


def test_sdk_and_cli_payloads_match(client, planned, monkeypatch):
    from dataclasses import replace
    from inspire import DatasetMount
    from inspire.platform.web.browser_api.datasets import DatasetValidation

    monkeypatch.setattr(
        "inspire.services.catalog.datasets.validate_dataset_mounts",
        lambda *a, **kw: [DatasetValidation(dataset="data", version="v1", ok=True, path="/data")],
    )
    spec = replace(
        planned,
        framework="tensorflow",
        auto_fault_tolerance=True,
        fault_tolerance_max_retry=3,
        fault_tolerance_retry_interval_sec=60,
        datasets=[DatasetMount("data", "v1")],
        envs={"MODE": "test"},
        description="experiment",
        keep_after_success_hours=1.5,
        keep_after_failure_hours=2,
        public_path_readonly=True,
        enable_notification=True,
        exclude_nodes=["node-a"],
        specified_nodes=["node-b"],
        shm_gib=64,
        max_time_hours=2,
    )
    # Both adapters resolve the same catalog snapshot; only platform I/O is fake.
    platform_project = SimpleNamespace(project_id="p", name="project", priority_name="HIGH",
                                       priority_level="", member_remain_budget=None, remain_budget=None)
    platform_group = {
        "id": "g", "name": "group", "support_job_type_list": '["distributed_training"]'
    }
    platform_price = {
        "quota_id": "q", "gpu_count": 1, "cpu_count": 20, "memory_size_gib": 200,
        "gpu_info": {"gpu_type": "GPU"},
    }
    platform_image = SimpleNamespace(name="image", version="v1", image_id="i", url="registry/image:v1",
                                    source="SOURCE_PUBLIC", visibility="VISIBILITY_PRIVATE",
                                    status="", framework="")
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.images.list_images_by_source",
        lambda source, **kw: [platform_image] if source == "private" else [],
    )
    monkeypatch.delattr(client.projects, "_all")
    monkeypatch.delattr(client.compute_groups, "_all")
    monkeypatch.delattr(client.jobs, "_quota_rows")
    monkeypatch.delattr(client.images, "get")
    monkeypatch.setattr("inspire.platform.web.browser_api.list_projects", lambda **kw: [platform_project])
    monkeypatch.setattr("inspire.platform.web.browser_api.availability.list_compute_groups", lambda **kw: [platform_group])
    monkeypatch.setattr("inspire.platform.web.browser_api.notebooks.get_resource_prices", lambda **kw: [platform_price])
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.list_images_by_source",
        lambda source, **kw: [platform_image] if source == "private" else [],
    )
    monkeypatch.setattr("inspire.platform.web.browser_api.get_image_detail", lambda **kw: platform_image)
    public, sdk_plan = client.jobs._plan(spec)
    from click.testing import CliRunner
    from inspire.cli.main import main as cli_main
    from inspire.cli.commands.job import job_create
    from inspire.config import Config

    ws = client.workspaces.get(spec.workspace)
    project, _ = client.projects._all(ws)[0]
    group, _ = client.compute_groups._all(ws)[0]
    _, resolved_quota = client.jobs._quota_rows(ws, group)[0]
    image = client.images.get(spec.image, workspace=ws.ref)
    monkeypatch.setattr(Config, "from_files_and_env", classmethod(lambda cls, **kw: (client._config, {})))
    monkeypatch.setattr(job_create, "get_web_session", lambda: client._transport.session)
    monkeypatch.setattr(job_create, "select_workspace_id", lambda **kw: ws.ref.key)
    monkeypatch.setattr(job_create, "is_fair_scheduling_workspace", lambda *a: False)

    def resolve_quota(*, spec, workspace_id, group_override, **kwargs):
        assert workspace_id == ws.ref.key and group_override == group.name
        assert (spec.gpu_count, spec.cpu_count, spec.memory_gib) == (1, 20, 200)
        return resolved_quota

    monkeypatch.setattr(job_create, "resolve_quota", resolve_quota)
    api = job_create.browser_api_module
    monkeypatch.setattr(api, "list_projects", lambda **kw: [platform_project])
    monkeypatch.setattr(api, "check_scheduling_health", lambda **kw: {})
    monkeypatch.setattr(api, "select_project", lambda *a, **kw: (platform_project, None))
    monkeypatch.setattr(api, "get_train_schedule_capabilities", lambda *a, **kw: SimpleNamespace(specified_nodes=True))

    captured = []
    real_build = job_create.job_submit.build_training_job_plan

    def record_plan(**kwargs):
        plan = real_build(**kwargs)
        captured.append(plan.create_kwargs)
        return plan

    monkeypatch.setattr(job_create.job_submit, "build_training_job_plan", record_plan)
    result = CliRunner().invoke(cli_main, [
        "job", "create", "--dry-run", "--name", "job", "--command", "python train.py",
        "--workspace", ws.name, "--project", project.name, "--group", group.name,
        "--quota", "1,20,200", "--image", image.name, "--framework", "tensorflow",
        "--priority", "4", "--nodes", "1", "--auto-fault-tolerance",
        "--fault-tolerance-max-retry", "3", "--fault-tolerance-retry-interval", "60",
        "--dataset", "data:v1", "--env", "MODE=test", "--description", "experiment",
        "--keep-after-success", "1.5", "--keep-after-failure", "2",
        "--public-path-readonly", "--enable-notification", "--exclude-node", "node-a",
        "--specified-node", "node-b", "--shm-size", "64", "--max-time", "2",
    ])
    assert result.exit_code == 0, result.output
    assert captured == [sdk_plan.create_kwargs]
    assert public.envs_count == 1 and public.shm == 64
    assert "python train.py" not in public.summary


def test_list_fetches_only_needed_pages(client, monkeypatch):
    from inspire.platform.web.browser_api.jobs import JobInfo
    from dataclasses import fields

    ws = Resource("ws", WorkspaceRef("ws", "alpha", client.base_url, "ws-test", "ws-test"))
    monkeypatch.setattr(client.workspaces, "get", lambda _: ws)
    template = {f.name: "" for f in fields(JobInfo)}
    calls = []

    def listing(**kw):
        calls.append(kw["page_num"])
        start = (kw["page_num"] - 1) * 100
        return [
            JobInfo(**{**template, "job_id": str(i), "name": str(i), "status": "job_running"})
            for i in range(start, start + 100)
        ], 10000

    monkeypatch.setattr("inspire.platform.web.browser_api.jobs.list_jobs", listing)
    first = client.jobs.list(workspace="ws", limit=3)
    second = client.jobs.list(workspace="ws", limit=3, cursor=first.next_cursor)
    assert [x.name for x in first.items + second.items] == [str(i) for i in range(6)]
    assert calls == [1, 1]
    assert first.total is None
    with pytest.raises(ValidationError):
        client.jobs.list(workspace="ws", status="RUNNING", cursor=first.next_cursor)


@pytest.fixture
def planned(client, monkeypatch):
    from inspire.sdk import (
        ProjectRef,
        ComputeGroupRef,
        ImageRef,
        Image,
        JobCreateSpec,
        QuotaRef,
        QuotaOption,
    )
    from inspire.services.catalog.quotas import ResolvedQuota

    ws = Resource(
        "workspace", client.workspaces._make_ref(WorkspaceRef, "workspace", "ws-test", "ws-test")
    )
    project = Resource("project", client.projects._make_ref(ProjectRef, "project", "p", "ws-test"))
    group = Resource("group", client.compute_groups._make_ref(ComputeGroupRef, "group", "g", "ws-test"))
    img = Image(
        "image:v1",
        client.images._make_ref(ImageRef, "image:v1", "i", "ws-test"),
        "private",
        "registry/image:v1",
    )
    quota = Quota(1, 20, 200)
    option = QuotaOption(
        "1,20,200", client.jobs._make_ref(QuotaRef, "1,20,200", "q", "ws-test"), quota, group.ref, "GPU"
    )
    resolved = ResolvedQuota(
        "q", "g", "group", 1, 20, 200, "GPU", {"gpu_info": {"gpu_type": "GPU"}}
    )
    monkeypatch.setattr(client.workspaces, "get", lambda _: ws)
    monkeypatch.setattr(
        client.projects, "_all", lambda _: [(project, SimpleNamespace(priority_name="HIGH"))]
    )
    monkeypatch.setattr(
        client.compute_groups,
        "_all",
        lambda _: [(group, {"support_job_type_list": '["distributed_training"]'})],
    )
    monkeypatch.setattr(client.images, "get", lambda *a, **kw: img)
    monkeypatch.setattr(client.jobs, "_quota_rows", lambda *a: [(option, resolved)])
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.workspaces.is_fair_scheduling_workspace", lambda *a: False
    )
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.availability.get_quota_priority_levels",
        lambda *a, **kw: {},
    )
    return JobCreateSpec(
        "job", ws.ref, project.ref, group.ref, quota, img.ref, "python train.py", priority=4
    )


def test_create_plan_and_single_dispatch(client, planned, monkeypatch):
    calls = []

    def create(**kw):
        calls.append(kw["payload"])
        return {"job_id": "created"}

    monkeypatch.setattr("inspire.platform.web.browser_api.jobs.create_training_job", create)
    monkeypatch.setattr(client.jobs, "get", lambda *a, **kw: pytest.fail("post-create read"))
    preview = client.jobs.plan(planned)
    assert preview.quota == Quota(1, 20, 200) and calls == []
    handle = client.jobs.create(planned)
    assert handle.ref.key == "created" and len(calls) == 1
    assert preview.create_kwargs == calls[0]
    assert preview.to_dict()["image"] == preview.image.name
    assert "python train.py" not in repr(preview)
    assert "python train.py" not in repr(planned)
    assert "python train.py" not in preview.summary


def test_create_missing_acknowledgement_is_uncertain(client, planned, monkeypatch):
    calls = []

    def once(**kw):
        calls.append(1)
        return {}

    monkeypatch.setattr("inspire.platform.web.browser_api.jobs.create_training_job", once)
    identifier = "00000000-0000-4000-8000-000000000001"
    with pytest.raises(SubmissionUncertainError) as exc:
        client.jobs.create(planned, operation_id=identifier)
    assert calls == [1] and exc.value.operation_id == identifier


def test_plan_rejects_unsupported_group(client, planned, monkeypatch):
    group = client.compute_groups._all(None)[0][0]
    monkeypatch.setattr(
        client.compute_groups,
        "_all",
        lambda _: [(group, {"support_job_type_list": '["interactive_modeling"]'})],
    )
    with pytest.raises(ValidationError, match="does not support"):
        client.jobs.plan(planned)


def test_declared_mutation_is_not_replayed(client, monkeypatch):

    calls = []

    def fail(*a, **kw):
        calls.append(1)
        client._transport._write["sent"] = True
        raise TransientAPIError("busy")

    monkeypatch.setattr(client._transport, "_once", fail)
    from inspire.sdk import MutationUncertainError

    with pytest.raises(MutationUncertainError) as caught:
        with client._transport.single_send():
            client._transport.request("GET", "/api/v2/train?Action=GetUnknownSideEffect")
    assert calls == [1]
    assert str(caught.value.__cause__) == "busy"
    assert not caught.value.retryable


def test_transport_returns_business_envelope_untouched(client, monkeypatch):
    payload = {
        "ResponseMetadata": {"Error": {"Code": "InvalidParameter", "Message": "platform message"}}
    }
    monkeypatch.setattr(client._transport, "_once", lambda *a, **kw: payload)
    assert client._transport.request("POST", "/arbitrary") is payload


def test_refresh_cooldown_has_structured_deadline(client, monkeypatch):
    from inspire.sdk import AuthenticationCooldownError
    from inspire.platform.web.session.models import AuthenticationError as PlatformAuthError
    from contextlib import nullcontext

    deadline = time.time() + 60

    def fail(*args, **kw):
        error = PlatformAuthError("Account alpha blocked; update credentials with inspire account set --password.")
        error.retry_at = deadline
        raise error

    monkeypatch.setattr("inspire.platform.web.session.models.WebSession.load", lambda **kw: None)
    monkeypatch.setattr(
        "inspire.platform.web.session.refresh_lock.exclusive_session_refresh",
        lambda *a, **kw: nullcontext(),
    )
    monkeypatch.setattr("inspire.platform.web.session.auth.renew_web_session_without_credentials", lambda _: None)
    monkeypatch.setattr("inspire.platform.web.session.auth.login_without_browser", fail)
    client._transport.allow_browser = True
    with pytest.raises(AuthenticationCooldownError) as caught:
        client._transport._refresh()
    assert caught.value.retry_at == deadline
    assert str(caught.value) == "Account alpha blocked; update credentials with inspire account set --password."


@pytest.mark.parametrize("status", [401, 302, 429, 500, 503])
def test_http_create_errors_do_not_replay(client, monkeypatch, status):
    calls = []

    def request(*a, **kw):
        calls.append(kw)
        return SimpleNamespace(status_code=status)

    client._transport._http = SimpleNamespace(request=request, close=lambda: None)
    monkeypatch.setattr("inspire.platform.web.session.requests._configure", lambda *a: None)
    with pytest.raises(TransportError if status == 429 else SubmissionUncertainError):
        with client._transport.single_send(create=True):
            from inspire.platform.web.session.envelope import _v2_result

            _v2_result(client._transport.request("POST", "/api/v2/train?Action=CreateJobConsole"))
    assert len(calls) == 1 and calls[0]["allow_redirects"] is False


def test_partial_image_catalog_cannot_resolve_unique_name(client, monkeypatch):
    from inspire.sdk import ResolutionIncompleteError

    ws = Resource("ws", WorkspaceRef("ws", "alpha", client.base_url, "ws-test", "ws-test"))
    monkeypatch.setattr(client.workspaces, "get", lambda _: ws)

    def listing(source, **kw):
        if source == "private":
            raise RuntimeError("unreadable")
        return [SimpleNamespace(name="image", version="v1", image_id="i", url="r/i")]

    monkeypatch.setattr("inspire.platform.web.browser_api.list_images_by_source", listing)
    with pytest.raises(ResolutionIncompleteError):
        client.images.get("image:v1", workspace=ws.ref)


def test_single_send_rejects_second_request_and_restores_reads(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        client._transport, "_once", lambda *a, **kw: calls.append(a) or {"hello": "world"}
    )
    with client._transport.single_send():
        assert client._transport.request("GET", "/anything") == {"hello": "world"}
        with pytest.raises(RuntimeError, match="exactly one"):
            client._transport.request("POST", "/another")
    assert len(calls) == 1
    client._transport.request("POST", "/another")
    assert len(calls) == 2


def test_cursor_encodes_query_by_plain_equality(client):
    import base64
    import json

    page = client.jobs._page(list(range(25)), query=("workspace", "running"))
    decoded = json.loads(base64.urlsafe_b64decode(page.next_cursor))
    assert decoded == {
        "offset": 20,
        "query": ["alpha", client.base_url, "Jobs", ["workspace", "running"]],
    }
    assert client.jobs._page(
        list(range(25)), cursor=page.next_cursor, query=("workspace", "running")
    ).items == tuple(range(20, 25))


@pytest.mark.parametrize("window,minutes", [("30m", 30), ("2h", 120), ("1d", 1440), (" 2H ", 120)])
def test_window_parsing_shared(window, minutes):
    from inspire.services.job.job_logs import window_to_minutes
    from inspire.cli.commands.job.job_logs import window_to_minutes as cli_window

    assert window_to_minutes(window) == cli_window(window) == minutes


@pytest.mark.parametrize("window", ["0m", "oops", "-2h", "1s"])
def test_invalid_log_window(window):
    from inspire.services.job.job_logs import window_to_minutes

    with pytest.raises(ValueError):
        window_to_minutes(window)


@pytest.mark.parametrize("selection", [{"tail": 2}, {"head": 2}, {"limit": 2}])
def test_sdk_log_selection_matches_cli(client, monkeypatch, selection):
    from inspire.cli.commands.job.job_logs import select_job_logs

    ref = JobRef("job", "alpha", client.base_url, "key", "ws-test")
    monkeypatch.setattr(
        client.jobs, "get", lambda *a, **kw: Job("job", ref, "RUNNING", "job_running")
    )
    rows = [{"timestamp_ms": x, "message": str(x), "pod_name": "worker"} for x in (3, 1, 2)]
    calls = []

    def fetch(**kwargs):
        calls.append(kwargs)
        return rows, 3

    monkeypatch.setattr("inspire.platform.web.browser_api.jobs.list_train_job_logs", fetch)
    monkeypatch.setattr(
        "inspire.services.job.job_events.list_all_job_instances",
        lambda *a, **kw: [{"name": "worker"}],
    )
    result = client.jobs.logs(ref, instance=["worker"], window="30m", **selection)
    cli = select_job_logs(
        rows,
        total=3,
        tail=selection.get("tail"),
        head=selection.get("head"),
        record_limit=selection.get("limit", 100),
        all_output=False,
    )
    assert [row["message"] for row in result.items] == [row["message"] for row in cli.logs]
    assert calls[0]["page_size"] == max(
        selection.get("limit", 100), selection.get("tail", 0), selection.get("head", 0)
    )
    assert calls[0]["end_timestamp_ms"] - calls[0]["start_timestamp_ms"] == 1800000


def test_status_batch_and_command(client, monkeypatch):
    refs = [JobRef(n, "alpha", client.base_url, n, "ws-test") for n in ("first", "second")]
    calls = []

    def detail(key, **kw):
        calls.append(key)
        return {"name": key, "status": "job_running", "command": "echo hello"}

    batch_calls = []

    def batch(keys, **kw):
        batch_calls.append((keys, kw["workspace_id"]))
        return {key: {"status": "job_running", "priority": 6, "command": "echo hello"}
                for key in reversed(keys)}

    monkeypatch.setattr("inspire.platform.web.browser_api.jobs.get_job_detail_v2", detail)
    monkeypatch.setattr("inspire.platform.web.browser_api.jobs.list_jobs_by_ids", batch)
    result = client.jobs.status([refs[1], refs[0], refs[1]])
    assert [j.name for j in result] == ["second", "first", "second"]
    assert batch_calls == [(["second", "first", "second"], "ws-test")]
    for job in result:
        assert job.view == {"name": job.name, "status": "RUNNING", "priority": 6}
        assert job.to_dict() == job.view
        assert job.raw["command"] == "echo hello"
    assert client.jobs.command(refs[0]) == "echo hello"
    assert calls == ["first"]


def test_training_job_status_workspace_groups(client, monkeypatch):
    refs = [JobRef("Training", "alpha", client.base_url, "same-key", ws)
            for ws in ("ws-first", "ws-second")]
    calls = []

    def batch(keys, *, workspace_id, session):
        calls.append(workspace_id)
        return {"same-key": {"workspace_id": workspace_id, "priority": len(calls)}}

    monkeypatch.setattr("inspire.platform.web.browser_api.jobs.list_jobs_by_ids", batch)
    result = client.jobs.status(refs)
    assert [job.view["priority"] for job in result] == [1, 2]
    assert [job.ref for job in result] == refs
    assert calls == ["ws-first", "ws-second"]
    assert client.jobs.status([]) == ()
    assert len(calls) == 2


@pytest.mark.parametrize("record", [None, {"workspace_id": "wrong-workspace"}])
def test_training_job_status_rejects_missing_or_wrong_workspace(client, monkeypatch, record):
    from inspire import ResourceNotFoundError

    ref = JobRef("Training", "alpha", client.base_url, "job-test", "ws-test")
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.jobs.list_jobs_by_ids",
        lambda *a, **kw: {} if record is None else {ref.key: record},
    )
    with pytest.raises(ResourceNotFoundError if record is None else ValidationError):
        client.jobs.status([ref])


def test_events_filters_and_structured_instances(client, monkeypatch):
    ref = JobRef("job", "alpha", client.base_url, "key", "ws-test")
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.list_job_instances",
        lambda *a, **kw: (
            [
                {
                    "name": "worker",
                    "rank": 0,
                    "status": "Running",
                    "node_name": "node-a",
                    "role": "worker",
                }
            ],
            1,
        ),
    )
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.jobs.list_job_events",
        lambda *a, **kw: [{"type": "Normal", "reason": "Created", "id": "1"}],
    )
    captured = []

    def pod_events(key, names, **kw):
        captured.append(names)
        return [
            {"event_type": "Warning", "reason": "Unschedulable", "id": "2", "object_id": "worker"}
        ]

    monkeypatch.setattr(
        "inspire.platform.web.browser_api.jobs.list_job_instance_events", pod_events
    )
    assert len(client.jobs.events(ref).items) == 2
    event = client.jobs.events(ref, type="warning", reason="SCHED", instance="worker").items[0]
    assert event["instance"] == "worker" and captured[-1] == ["worker"]
    assert client.jobs.events(ref, workload_level=True).items[0]["reason"] == "Created"
    with pytest.raises(ValidationError):
        client.jobs.events(ref, workload_level=True, instance="worker")
    instance = client.jobs.instances(ref)[0]
    assert (instance.label, instance.status, instance.node, instance.rank) == (
        "worker",
        "Running",
        "node-a",
        0,
    )


def test_metrics_extraction(client, monkeypatch):
    from inspire.platform.web.browser_api.metrics import MetricGroup, MetricSample

    ref = JobRef("job", "alpha", client.base_url, "key", "ws-test")
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.jobs.get_job_detail_v2",
        lambda *a, **kw: {"logic_compute_group_id": "group"},
    )
    calls = []
    groups = [MetricGroup("worker", "cpu_usage_rate", "CPU", [MetricSample(100, 0.5)])]

    def fetch(**kw):
        calls.append(kw)
        return groups

    monkeypatch.setattr(
        "inspire.platform.web.browser_api.metrics.get_resource_metrics_by_time", fetch
    )
    assert client.jobs.metrics(ref, metric="cpu", end="3600", window="30m") == tuple(groups)
    assert calls[0]["metric_types"] == ["cpu_usage_rate"]
    assert (
        calls[0]["start_timestamp"],
        calls[0]["end_timestamp"],
        calls[0]["interval_second"],
    ) == (1800, 3600, 60)


def test_read_and_write_http_validation_message(client, monkeypatch):

    client._transport._http = SimpleNamespace(
        request=lambda *a, **kw: SimpleNamespace(
            status_code=422, text="platform detail " + "x" * 1000
        ),
        close=lambda: None,
    )
    monkeypatch.setattr("inspire.platform.web.session.requests._configure", lambda *a: None)
    with pytest.raises(ValidationError, match="HTTP 422: platform detail") as error:
        client._transport.request("POST", "/v1/anything")
    assert len(str(error.value)) < 520
    with pytest.raises(ValidationError, match="HTTP 422: platform detail"):
        with client._transport.single_send():
            client._transport.request("POST", "/v1/anything")


def test_create_custom_operation_id(client, planned, monkeypatch):
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.jobs.create_training_job", lambda **kw: {"job_id": "new"}
    )
    assert (
        client.jobs.create(planned, operation_id="run/experiment-1").operation_id
        == "run/experiment-1"
    )
    with pytest.raises(ValidationError, match="non-empty"):
        client.jobs.create(planned, operation_id="")


def test_failed_http_preparation_is_not_submission_uncertainty(client, monkeypatch):
    monkeypatch.setattr(
        "inspire.platform.web.session.requests.build_requests_session",
        lambda *a: (_ for _ in ()).throw(ValueError("cannot prepare session")),
    )
    with pytest.raises(ValueError, match="cannot prepare session") as error:
        with client._transport.single_send(create=True):
            client._transport.request("POST", "/write")
    assert not isinstance(error.value, SubmissionUncertainError)


def test_cached_session_does_not_require_login_username(client):
    client._transport._session.login_username = "different-login-name"
    assert (
        client._transport._validate_session(client._transport._session)
        is client._transport._session
    )


def test_short_intermediate_pages_and_raw_status(client, monkeypatch):
    from inspire.platform.web.browser_api.jobs import JobInfo
    from dataclasses import fields

    ws = Resource("ws", WorkspaceRef("ws", "alpha", client.base_url, "ws-test", "ws-test"))
    monkeypatch.setattr(client.workspaces, "get", lambda _: ws)
    template = {f.name: "" for f in fields(JobInfo)}

    def listing(**kw):
        page = kw["page_num"]
        return [
            JobInfo(**{**template, "job_id": str(page), "name": str(page), "status": "job_running"})
        ], 101

    monkeypatch.setattr("inspire.platform.web.browser_api.jobs.list_jobs", listing)
    assert [j.name for j in client.jobs.list(workspace="ws", status="JOB_RUNNING").items] == [
        "1",
        "2",
    ]
    assert len(client.jobs.list(workspace="ws", status="running").items) == 2
    with pytest.raises(TypeError):
        client.jobs.list(workspace="ws", owner="self")


def test_follow_events_and_logs_stop_at_terminal(client, monkeypatch):
    from inspire import EventResult, LogResult

    ref = JobRef("job", "alpha", client.base_url, "key", "ws-test")
    monkeypatch.setattr("inspire.sdk.jobs.time.sleep", lambda _: None)
    states = iter(["RUNNING", "SUCCEEDED"])
    monkeypatch.setattr(client.jobs, "get", lambda *a, **k: Job("job", ref, next(states), ""))
    event = {"id": "1", "reason": "Started"}
    monkeypatch.setattr(client.jobs, "events", lambda *a, **kw: EventResult((event,)))
    assert [batch.items for batch in client.jobs.follow_events(ref)] == [(event,)]
    states = iter(["RUNNING", "SUCCEEDED"])
    calls = []

    def logs(*a, **kw):
        calls.append(1)
        rows = ({"message": "hello", "log_id": "1"},)
        if len(calls) == 3:
            rows += ({"message": "last", "log_id": "2"},)
        return LogResult("hello", ("worker",), "start", "end", False, len(rows), rows)

    monkeypatch.setattr(client.jobs, "logs", logs)
    updates = list(client.jobs.follow_logs(ref))
    assert [u.text for u in updates] == ["hello", "last"]
    assert len(calls) == 3


def test_quota_all_training_groups_and_empty(client, planned, monkeypatch):
    from inspire import ComputeGroupRef

    group, _ = client.compute_groups._all(None)[0]
    empty = Resource(
        "empty", ComputeGroupRef("empty", "alpha", client.base_url, "empty", "ws-test")
    )
    excluded = Resource(
        "notebook", ComputeGroupRef("notebook", "alpha", client.base_url, "other", "ws-test")
    )
    monkeypatch.setattr(
        client.compute_groups,
        "_all",
        lambda _: [
            (group, {}),
            (empty, {}),
            (excluded, {"support_job_type_list": '["interactive_modeling"]'}),
        ],
    )
    original = client.jobs._quota_rows
    calls = []

    def quota_rows(ws, selected):
        calls.append(selected.name)
        return [] if selected.name == "empty" else original(ws, selected)

    monkeypatch.setattr(client.jobs, "_quota_rows", quota_rows)
    result = client.jobs.quotas(workspace="ws", include_empty=True)
    assert calls == ["group", "empty"]
    assert result.items[-1].quota is None
    assert len(client.jobs.quotas(workspace="ws", group="GRO").items) == 1


def test_registry_url_uses_cli_create_semantics(client, planned, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(
        client.images,
        "get",
        lambda *a, **kw: pytest.fail("registry URL must not require a catalog match"),
    )
    _, plan = client.jobs._plan(replace(planned, image="registry/project/image:v2"))
    assert plan.create_kwargs["framework_config"][0]["image"] == "registry/project/image:v2"


def test_unknown_read_retries_and_browser_requires_opt_in(client, monkeypatch):
    from requests.exceptions import ConnectionError

    calls = []

    def once(*args):
        calls.append(args[4])
        if len(calls) % 2:
            raise ConnectionError("disconnected")
        return ["v1", "body"]

    monkeypatch.setattr(client._transport, "_once", once)
    monkeypatch.setattr("inspire.platform.web.transport.time.sleep", lambda _: None)
    assert client._transport.request("POST", "/unregistered/v1") == ["v1", "body"]
    assert calls == [False, False]
    client._transport.allow_browser = True
    assert client._transport.request("POST", "/unregistered/v1") == ["v1", "body"]
    assert calls == [False, False, False, True]


def test_read_refreshes_session_only_once(client, monkeypatch):
    refreshes = []
    calls = []
    client._transport.allow_browser = True

    def fail(*args):
        calls.append(1)
        raise SessionExpiredError("expired again")

    monkeypatch.setattr(client._transport, "_once", fail)
    monkeypatch.setattr(client._transport, "_refresh", lambda: refreshes.append(1))
    monkeypatch.setattr("inspire.platform.web.transport.time.sleep", lambda _: None)
    with pytest.raises(AuthenticationError, match="expired again"):
        client._transport.request("POST", "/read")
    assert len(calls) == 2 and refreshes == [1]


def test_operation_keeps_original_validation_message(client, monkeypatch):
    monkeypatch.setattr(
        client.workspaces,
        "_all",
        lambda: (_ for _ in ()).throw(ValueError("precise platform reason")),
    )
    with pytest.raises(ValidationError, match="precise platform reason"):
        client.workspaces.list()


def test_services_and_sdk_do_not_import_cli_or_ui_dependencies():
    root = Path(__file__).resolve().parents[1] / "inspire"
    violations = []
    private_services = []
    for package in ("services", "sdk", "platform"):
        for path in (root / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imports = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports[alias.asname or alias.name.split(".")[0]] = (
                            alias.name if alias.asname else alias.name.split(".")[0]
                        )
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    for alias in node.names:
                        imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"

            def qualified_name(node):
                if isinstance(node, ast.Name):
                    return imports.get(node.id, node.id)
                if isinstance(node, ast.Attribute):
                    return f"{qualified_name(node.value)}.{node.attr}"
                return ""

            for node in ast.walk(tree):
                if package == "sdk" and isinstance(node, ast.Attribute):
                    if node.attr.startswith("_") and qualified_name(node.value).startswith(
                        "inspire.services."
                    ):
                        private_services.append(f"{path.relative_to(root)}:{node.lineno}")
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level:
                        parts = list(path.relative_to(root.parent).parts[:-1])
                        module = ".".join(parts[:len(parts) - node.level + 1] + [module]).rstrip(".")
                    modules = [module, *(f"{module}.{alias.name}" for alias in node.names)]
                    if package == "sdk" and module.startswith("inspire.services."):
                        if any(alias.name.startswith("_") for alias in node.names):
                            private_services.append(f"{path.relative_to(root)}:{node.lineno}")
                forbidden = ("inspire.cli", "click", "rich", "playwright") if package != "platform" else ()
                if package in ("platform", "services"):
                    forbidden += ("inspire.sdk",)
                if any(
                    name == prefix or name.startswith(prefix + ".")
                    for name in modules
                    for prefix in forbidden
                ):
                    violations.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not violations, "Forbidden imports in shared layers: " + ", ".join(violations)
    assert not private_services, "Private service names in SDK: " + ", ".join(private_services)


@pytest.mark.parametrize("allow_browser", [False, True])
def test_read_retries_json_decode_failure(client, monkeypatch, allow_browser):
    client._transport.allow_browser = allow_browser
    calls = []
    payload = {"Result": {"ok": True}}

    def once(*args):
        calls.append(args)
        if len(calls) == 1:
            raise ValueError("bad json")
        return payload

    monkeypatch.setattr(client._transport, "_once", once)
    monkeypatch.setattr("inspire.platform.web.transport.time.sleep", lambda _: None)
    assert client._transport.request("POST", "/api/v2/train?Action=ListJobs") == payload
    assert len(calls) == 2
    assert calls[0][4] is False
    assert calls[1][4] is allow_browser


@pytest.mark.parametrize("interval", ["invalid", "", None])
def test_metrics_rejects_invalid_interval(client, monkeypatch, interval):
    from inspire.platform.web.browser_api.metrics import INTERVAL_CHOICES

    monkeypatch.setattr(client.jobs, "_resolve", lambda *a: pytest.fail("unexpected lookup"))
    with pytest.raises(ValidationError) as error:
        client.jobs.metrics("job", interval=interval)
    assert all(choice in str(error.value) for choice in INTERVAL_CHOICES)


@pytest.fixture
def renewal(client, monkeypatch):
    from dataclasses import replace
    from inspire.platform.web.session import auth

    transport = client._transport
    old = transport.session
    new = replace(old, created_at=old.created_at + 1,
                  storage_state={"cookies": [{"name": "x", "value": "renewed"}]})
    monkeypatch.setattr(WebSession, "load", lambda **kw: old)
    monkeypatch.setattr(auth, "get_web_session", lambda **kw: pytest.fail("browser login invoked"))
    monkeypatch.setattr("inspire.platform.web.transport.time.sleep", lambda _: None)
    return auth, old, new


def fake_platform(client, monkeypatch, statuses):
    import requests

    calls = []
    replies = iter(statuses)
    http = requests.Session()

    def request(method, url, **kwargs):
        calls.append((url, http.cookies.get("x"), client._transport._write is not None))
        return SimpleNamespace(status_code=next(replies), json=lambda: {"Result": {"id": "user"}})

    monkeypatch.setattr(http, "request", request)
    client._transport._http = http
    return calls


def test_read_renews_sso_and_persists(client, monkeypatch, renewal):
    auth, old, new = renewal
    persisted, closed = [], []
    monkeypatch.setattr(auth, "renew_web_session_without_credentials", lambda current: new if current is old else None)
    monkeypatch.setattr(auth, "_persist", lambda session, **kw: persisted.append((session, kw)))
    monkeypatch.setattr(auth, "_login_with_cas_requests", lambda *a, **kw: pytest.fail("credentials submitted"))
    client._transport._browser = SimpleNamespace(close=lambda: closed.append(True))
    calls = fake_platform(client, monkeypatch, [401, 200])
    assert client._transport.request("POST", "/read")["Result"]["id"] == "user"
    assert [c[1] for c in calls] == ["fake", "renewed"]
    assert persisted == [(new, {"account": "alpha"})]
    assert closed == [True] and client._transport._browser is None


def test_read_credential_login_uses_guard(client, monkeypatch, renewal):
    from contextlib import contextmanager

    auth, _, new = renewal
    events = []

    @contextmanager
    def guard(username, password, *, account):
        assert (username, password, account) == ("test", "unused", "alpha")
        events.append("guard")
        yield
        events.append("success")

    def login(*args, **kwargs):
        assert events == ["guard"]
        events.append("cas")
        return new

    monkeypatch.setattr(auth, "renew_web_session_without_credentials", lambda _: None)
    monkeypatch.setattr(auth, "guarded_credential_submission", guard)
    monkeypatch.setattr(auth, "_login_with_cas_requests", login)
    calls = fake_platform(client, monkeypatch, [401, 200])
    client._transport.request("POST", "/read")
    assert events == ["guard", "cas", "success"]
    assert len(calls) == 2 and client._transport.session is new


@pytest.mark.parametrize("allow_browser", [False, True])
@pytest.mark.parametrize("sso_raises", [False, True])
def test_refresh_failure_browser_gate(client, monkeypatch, renewal, allow_browser, sso_raises):
    auth, _, new = renewal
    browser_calls = []
    credential_calls = []
    client._transport.allow_browser = allow_browser

    def renew(_):
        if sso_raises:
            raise RuntimeError("SSO unavailable")
        return None

    def fail(*args, **kwargs):
        credential_calls.append(True)
        raise RuntimeError("CAS unavailable")

    monkeypatch.setattr(auth, "renew_web_session_without_credentials", renew)
    monkeypatch.setattr(auth, "login_without_browser", fail)
    monkeypatch.setattr(auth, "get_web_session", lambda **kw: browser_calls.append(kw) or new)
    calls = fake_platform(client, monkeypatch, [401, 200])
    if sso_raises:
        with pytest.raises(AuthenticationError, match="SSO unavailable"):
            client._transport.request("POST", "/read")
        assert credential_calls == [] and browser_calls == [] and len(calls) == 1
    elif allow_browser:
        client._transport.request("POST", "/read")
        assert browser_calls == [{"force_refresh": True, "account": "alpha"}]
        assert len(calls) == 2
    else:
        with pytest.raises(AuthenticationError, match="CAS unavailable"):
            client._transport.request("POST", "/read")
        assert browser_calls == [] and len(calls) == 1


@pytest.mark.parametrize("allow_browser", [False, True])
def test_real_login_guard_blocks_repeated_sdk_login(client, monkeypatch, renewal, allow_browser):
    from inspire.sdk import AuthenticationCooldownError

    auth, _, _ = renewal
    attempts = []
    client._transport.allow_browser = allow_browser
    monkeypatch.setattr(auth, "renew_web_session_without_credentials", lambda _: None)

    def fail(*args, **kwargs):
        attempts.append(1)
        raise auth.AuthenticationError("CAS rejected submission")

    monkeypatch.setattr(auth, "_login_with_cas_requests", fail)
    with pytest.raises(AuthenticationCooldownError) as first:
        client._transport._refresh()
    with pytest.raises(AuthenticationCooldownError) as second:
        client._transport._refresh()
    assert first.value.retry_at == second.value.retry_at
    assert first.value.retry_at > time.time()
    assert attempts == [1]


@pytest.mark.parametrize("allow_browser", [False, True])
def test_browser_free_verification_error(client, monkeypatch, renewal, allow_browser):
    auth, _, _ = renewal
    client._transport.allow_browser = allow_browser
    monkeypatch.setattr(auth, "renew_web_session_without_credentials", lambda _: None)

    def challenge(*args, **kwargs):
        raise auth._CasVerificationRequired("The platform is asking for a verification code")

    monkeypatch.setattr(auth, "_login_with_cas_requests", challenge)
    with pytest.raises(AuthenticationError, match="asking for a verification code"):
        client._transport._refresh()
    from inspire.platform.web.session.login_guard import block_file
    assert not block_file("alpha").exists()


@pytest.mark.parametrize("idle", [None, 61, 60, 59])
def test_single_send_probes_only_when_idle(client, monkeypatch, idle):
    monkeypatch.setattr("inspire.platform.web.transport.time.monotonic", lambda: 1000)
    client._transport._last_success = None if idle is None else 1000 - idle
    probe = idle is None or idle >= 60
    calls = fake_platform(client, monkeypatch, [200, 200] if probe else [200])
    with client._transport.single_send(create=True):
        client._transport.request("POST", "/write")
    assert [c[0].removeprefix(client.base_url) for c in calls] == (
        ["/api/v2/user?Action=GetUserDetail", "/write"] if probe else ["/write"]
    )
    assert [c[2] for c in calls] == ([False, True] if probe else [True])
    assert client._transport._last_success == 1000


def test_single_send_probe_renews_before_only_write(client, monkeypatch, renewal):
    auth, _, new = renewal
    client._transport._last_success = None
    monkeypatch.setattr(auth, "renew_web_session_without_credentials", lambda _: new)
    calls = fake_platform(client, monkeypatch, [401, 200, 200])
    with client._transport.single_send(create=True):
        client._transport.request("POST", "/write")
    assert [c[1:] for c in calls] == [("fake", False), ("renewed", False), ("renewed", True)]
    assert [c[0].split("?")[-1] for c in calls[:2]] == ["Action=GetUserDetail"] * 2


@pytest.mark.parametrize("newer", [False, True])
def test_refresh_adopts_only_newer_cache(client, monkeypatch, renewal, newer):
    from dataclasses import replace

    auth, old, new = renewal
    cached = new if newer else replace(old, created_at=old.created_at - 1)
    monkeypatch.setattr(WebSession, "load", lambda **kw: cached)
    renewed = []
    monkeypatch.setattr(auth, "renew_web_session_without_credentials", lambda current: renewed.append(current) or new)
    client._transport._refresh()
    assert client._transport.session is new
    assert renewed == ([] if newer else [old])


def test_failed_probe_does_not_dispatch_write(client, monkeypatch, renewal):
    auth, _, _ = renewal
    client._transport._last_success = None
    monkeypatch.setattr(auth, "renew_web_session_without_credentials", lambda _: None)

    def fail(*args, **kwargs):
        raise RuntimeError("login failed")

    monkeypatch.setattr(auth, "login_without_browser", fail)
    calls = fake_platform(client, monkeypatch, [401])
    with pytest.raises(AuthenticationError, match="login failed"):
        with client._transport.single_send(create=True):
            pytest.fail("write body entered after failed probe")
    assert len(calls) == 1 and calls[0][2] is False
    assert client._transport._write is None


def test_write_401_after_successful_probe_is_still_uncertain(client, monkeypatch):
    client._transport._last_success = None
    calls = fake_platform(client, monkeypatch, [200, 401])
    monkeypatch.setattr(client._transport, "_refresh", lambda: pytest.fail("write refreshed"))
    with pytest.raises(SubmissionUncertainError):
        with client._transport.single_send(create=True):
            client._transport.request("POST", "/write")
    assert [c[2] for c in calls] == [False, True]


@pytest.mark.parametrize("create", [False, True])
@pytest.mark.parametrize(
    "outcome",
    ["Conflict", "InternalError", "Throttling", 400, 403, 429, 401, 302, 500,
     "timeout", "bad-json", "invalid-response"],
)
def test_write_outcome_classification(client, monkeypatch, create, outcome):
    import requests
    from inspire.sdk import MutationUncertainError, TransportError
    from inspire.platform.web.session.envelope import _v2_result

    messages = {
        "Conflict": "当前状态（运行中）无法删除，请先停止后再删除",
        "InternalError": "模型源路径不存在或访问异常",
        "Throttling": "请求过于频繁",
    }
    calls = []
    causes = []
    http = requests.Session()

    def decode():
        if outcome == "bad-json":
            raise requests.exceptions.JSONDecodeError("Invalid JSON", "<html>", 0)
        if outcome == "invalid-response":
            raise ValueError("Invalid response")
        return {"ResponseMetadata": {"Error": {
            "Code": outcome, "Message": messages[outcome],
        }}}

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        if outcome == "timeout":
            raise requests.exceptions.ReadTimeout("Response lost")
        return SimpleNamespace(
            status_code=outcome if isinstance(outcome, int) else 200,
            text="platform rejection detail",
            json=decode,
        )

    monkeypatch.setattr(http, "request", request)
    monkeypatch.setattr(client._transport, "_http", http)
    monkeypatch.setattr(client._transport, "_refresh", lambda: pytest.fail("write refreshed"))
    client._transport.allow_browser = True
    uncertain = SubmissionUncertainError if create else MutationUncertainError
    expected = (
        ValidationError if outcome in ("Conflict", 400)
        else AuthenticationError if outcome == 403
        else TransportError if outcome in ("Throttling", 429)
        else uncertain
    )
    with pytest.raises(expected) as caught:
        with client._transport.single_send("f5-operation", create=create):
            payload = client._transport.request("POST", "/write")
            try:
                _v2_result(payload)
            except Exception as error:
                causes.append(error)
                raise

    assert type(caught.value) is expected
    assert caught.value.retryable is (expected is TransportError)
    assert len(calls) == 1
    assert client._transport._write is None
    if outcome in messages:
        message_error = caught.value.__cause__ if outcome == "InternalError" else caught.value
        assert str(message_error) == f"API error: {outcome}: {messages[outcome]}"
        assert caught.value.__cause__ is causes[0]
    elif outcome == 400:
        assert str(caught.value) == "HTTP 400: platform rejection detail"
        assert isinstance(caught.value.__cause__, ValidationError)
    elif expected is uncertain:
        assert caught.value.__cause__ is not None
        if create:
            assert caught.value.operation_id == "f5-operation"


@pytest.mark.parametrize("error_name", ["ValidationError", "AuthenticationError", "TransportError"])
@pytest.mark.parametrize("create", [False, True])
def test_single_send_preserves_definite_body_errors(client, monkeypatch, error_name, create):
    import inspire.sdk as sdk

    error = getattr(sdk, error_name)("already classified")
    calls = fake_platform(client, monkeypatch, [200])
    with pytest.raises(type(error)) as caught:
        with client._transport.single_send(create=create):
            client._transport.request("POST", "/write")
            raise error
    assert caught.value is error
    assert len(calls) == 1


def test_root_sdk_exports_match_static_type_checking_imports():
    import inspire
    import inspire.sdk as sdk

    tree = ast.parse(Path(inspire.__file__).read_text(encoding="utf-8"))
    exported = {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name)
        and node.test.id == "TYPE_CHECKING"
        for statement in node.body
        if isinstance(statement, ast.ImportFrom) and statement.module == "sdk"
        for alias in statement.names
    }
    assert exported == set(sdk.__all__)
    assert all(getattr(inspire, name) is getattr(sdk, name) for name in exported)


def test_training_string_and_typed_quota_create_identical_payload(client, planned, monkeypatch):
    from dataclasses import replace
    payloads = []

    def create(**kwargs):
        payloads.append(kwargs["payload"])
        return {"job_id": "created"}

    monkeypatch.setattr("inspire.platform.web.browser_api.jobs.create_training_job", create)
    quota_ref = client.jobs._quota_rows(None, None)[0][0].ref
    for quota in (planned.quota, "1,20,200", " 1, 20, 200 ", quota_ref):
        client.jobs.create(replace(planned, quota=quota))
    assert len(payloads) == 4
    assert all(payload == payloads[0] for payload in payloads)
    for quota in ("bad", "1,0,200", "-1,20,200"):
        with pytest.raises(ValidationError):
            client.jobs.create(replace(planned, quota=quota))
    assert len(payloads) == 4
