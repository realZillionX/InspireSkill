"""Offline CLI behavior contracts from origin/main, including adverse platform replies."""

from contextlib import contextmanager
import importlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import click
from click.testing import CliRunner
import pytest
import requests

from inspire.cli.context import Context
from inspire.cli.main import main
from inspire.config import Config
from inspire.platform.web.browser_api import jupyter_terminal as jt
from inspire.platform.web.browser_api import tensorboards as tb
from inspire.platform.web import pty_socket
from inspire.platform.web.session import WebSession
from inspire.platform.web.transport import Transport


@pytest.mark.parametrize("status", [403, 502])
@pytest.mark.parametrize("json_output", [False, True])
def test_r1_handshake_failure_keeps_cli_timeout_and_hint(monkeypatch, capsys, status, json_output):
    remote = importlib.import_module("inspire.cli.commands.notebook.remote_exec")

    @contextmanager
    def terminal(*args, **kwargs):
        yield SimpleNamespace(ws_url="wss://example.invalid/terminal")

    def broken(*args, **kwargs):
        raise pty_socket.JobShellError(f"WebSocket handshake failed: HTTP/1.1 {status}")

    monkeypatch.setattr(jt, "_jupyter_terminal", terminal)
    monkeypatch.setattr(pty_socket, "WebSocketClient", broken)
    ctx = Context()
    ctx.json_output = json_output
    code = remote.try_exec_via_jupyter_terminal(
        ctx,
        notebook_id="nb-test",
        command="echo ok",
        session=WebSession(storage_state={}, cookies={}, created_at=1),
        remote_cwd=None,
        env_exports="",
        timeout_s=60,
    )
    assert code == 14
    captured = capsys.readouterr()
    output = captured.out + captured.err
    if json_output:
        assert "JupyterTerminalUnreachable" in output
    else:
        assert "JupyterTerminal did not establish or complete the remote command." in output
    assert "--debug notebook exec" in output


@pytest.mark.parametrize("kind", ["hpc", "ray"])
@pytest.mark.parametrize("operation", ["list", "status"])
@pytest.mark.parametrize("status,expected", [("", "UNKNOWN"), ("Running", "RUNNING")])
@pytest.mark.parametrize("as_json", [False, True])
def test_ad1_cli_status_is_normalized(monkeypatch, kind, operation, status, expected, as_json):
    mod = importlib.import_module(f"inspire.cli.commands.{kind}.{kind}_commands")
    monkeypatch.setattr(
        Config,
        "from_files_and_env",
        classmethod(lambda cls, **kw: (Config(username="test", password="test"), {})),
    )
    monkeypatch.setattr(mod, "get_web_session", lambda: SimpleNamespace())
    monkeypatch.setattr(mod, "resolve_workspace_query_scope", lambda **kw: (["ws-test"], False))
    monkeypatch.setattr(mod, "workspace_name_map", lambda s: {"ws-test": "Room"})
    monkeypatch.setattr(mod, "_current_user_id", lambda s: "user-test")
    monkeypatch.setattr(mod, f"_resolve_{kind}_name_in_workspace", lambda *a, **kw: "job-test")
    row = SimpleNamespace(
        name="demo",
        status=status,
        created_at="",
        entrypoint="",
        raw={},
        project_name="",
        compute_group_name="",
        created_by_name="",
        workspace_id="ws-test",
    )
    monkeypatch.setattr(mod.browser_api_module, f"list_{kind}_jobs", lambda **kw: ([row], 1))
    monkeypatch.setattr(
        mod.browser_api_module,
        f"get_{kind}_job_detail",
        lambda *a, **kw: {"name": "demo", "status": status},
    )
    args = (["--json"] if as_json else []) + [kind, operation]
    if operation == "status":
        args += ["demo"]
    result = CliRunner().invoke(main, args + ["--workspace", "Room"])
    assert result.exit_code == 0, result.output
    if as_json:
        data = json.loads(result.output)["data"]
        if operation == "list":
            data = data["items"][0]
        assert data["status"] == expected
    else:
        assert expected in result.output
        assert "Running" not in result.output


@pytest.mark.parametrize("kind", ["hpc", "ray"])
@pytest.mark.parametrize("operation", ["list_item", "status"])
@pytest.mark.parametrize(
    "status", ["https://example.invalid/private/token", "job-12345678-1234-1234-1234-123456789abc"]
)
def test_r2_public_status_scrubs_urls_and_ids(kind, operation, status):
    mod = importlib.import_module(f"inspire.services.{kind}.{kind}_output")
    result = getattr(mod, f"public_{kind}_{operation}")({"name": "demo", "status": status})
    assert result["status"] == "UNKNOWN"
    assert status not in result["status"]


@pytest.mark.parametrize(
    "path", ["data/runs", "data/plugin/scalars/tags", "data/plugin/scalars/scalars"]
)
@pytest.mark.parametrize("status", [302, 404, 500])
def test_r3_tensorboard_application_http_contract(monkeypatch, path, status):
    from inspire.platform.web.session import requests as preparation

    session = WebSession(storage_state={}, base_url="https://example.invalid", created_at=1)
    owner = Transport(None, session.base_url, username="", cli_compat=True)
    owner.adopt_session(session)
    monkeypatch.setattr(
        owner,
        "_refresh_expired_session",
        Mock(side_effect=AssertionError("Unexpected session refresh")),
    )
    monkeypatch.setattr(tb, "get_transport", lambda s: owner)
    http = requests.Session()
    monkeypatch.setattr(preparation, "build_requests_session", lambda *a: http)
    calls = []

    def send(request, **kwargs):
        calls.append(request.url)
        response = requests.Response()
        response.request = request
        response.url = request.url
        response.status_code = 200 if request.url.endswith("/redirected") else status
        response._content = (
            b'["run"]' if response.status_code == 200 else b"gateway detail " + b"x" * 220
        )
        if response.status_code == 302:
            response.headers["Location"] = "/redirected"
        return response

    # Exercise requests redirect handling with an offline HTTP adapter.
    monkeypatch.setattr(http, "send", _REQUESTS_SEND.__get__(http))
    monkeypatch.setattr(http.get_adapter("https://"), "send", send)
    try:
        if status == 302:
            assert tb._tensorboard_get("https://example.invalid/board/", path, session) == ["run"]
            assert len(calls) == 2
        else:
            with pytest.raises(ValueError) as error:
                tb._tensorboard_get("https://example.invalid/board/", path, session)
            assert (
                str(error.value)
                == f"TensorBoard returned {status} for {path}: "
                + ("gateway detail " + "x" * 220)[:200]
            )
            assert len(calls) == 1
    finally:
        owner.close()
        http.close()


_REQUESTS_SEND = requests.Session.send


def test_ad2_serving_model_resolution_reads_complete_single_page(monkeypatch):
    from inspire.services.serving import serving_submission as service

    row = SimpleNamespace(
        model_id="model-test", name="demo", status="Ready", created_at="", latest_version="3"
    )
    listing = Mock(return_value=([row], 1))
    monkeypatch.setattr(service.browser_api_module, "list_models", listing)
    result = service.resolve_model_for_create(
        name="demo",
        workspace_id="ws-test",
        project_id="project-test",
        user_id="user-test",
        session=object(),
        resolve=lambda candidates: candidates[0]["id"],
    )
    assert result == ("model-test", 3, "demo")
    assert listing.call_count == 1
    assert listing.call_args.kwargs["page"] == 1
    assert listing.call_args.kwargs["page_size"] == 100


@pytest.mark.parametrize(
    "timeout,creation,capture_ms,creation_budget", [(600, 15, 600000, 600), (0.2, 15, 1000, 10)]
)
def test_r5_terminal_creation_has_separate_budget(
    monkeypatch, timeout, creation, capture_ms, creation_budget
):
    clock = [0.0]
    budgets = []
    monkeypatch.setattr(jt.time, "monotonic", lambda: clock[0])

    @contextmanager
    def terminal(*args, timeout_s):
        budgets.append(timeout_s)
        clock[0] += creation
        yield SimpleNamespace(ws_url="wss://example.invalid/terminal")

    capture = Mock(
        return_value=jt.JupyterCommandResult(
            returncode=0, output="ok", completed=True, marker="done"
        )
    )
    monkeypatch.setattr(jt, "_jupyter_terminal", terminal)
    monkeypatch.setattr(jt, "_capture_terminal_output", capture)
    result = jt._run_command_capture_in_notebook_sync(
        notebook_id="nb-test", command="echo ok", session=object(), timeout=timeout, marker="done"
    )
    assert result.completed
    assert budgets == [creation_budget]
    assert capture.call_args.kwargs["timeout_ms"] == capture_ms


@pytest.mark.parametrize(
    "field",
    ["name", "command", "image", "group", "quota", "workspace", "project", "image_type", "workers"],
)
def test_r6_ray_validates_before_resolving_workspace_and_project(monkeypatch, field):
    from inspire.cli.commands.ray import ray_commands as mod

    workspace = Mock(return_value=None)
    project = Mock()
    monkeypatch.setattr(mod, "select_workspace_id", workspace)
    monkeypatch.setattr(mod, "_resolve_project_id", project)
    values = dict(
        name="demo",
        command="python driver.py",
        image="ray",
        group="cpu",
        quota="0,4,16",
        workspace="Room",
        project="Project",
        image_type="SOURCE_PUBLIC",
        workers=("worker",),
        description="",
        priority=None,
        shm_size=None,
    )
    values[field] = () if field == "workers" else ""
    with pytest.raises(click.UsageError) as error:
        mod._assemble_create_body(
            Context(), config=Config(username="test", password="test"), session=object(), **values
        )
    assert error.value.exit_code == 2
    assert ("--worker" if field == "workers" else "--" + field.replace("_", "-")) in str(
        error.value
    )
    workspace.assert_not_called()
    project.assert_not_called()


@pytest.mark.parametrize("field", ["name", "url"])
def test_r7_save_image_skips_null_catalog_fields(field):
    from inspire.services.notebook.notebooks import resolve_saved_image_id

    bad = dict(
        name="unrelated", url="registry/unrelated:v1", version=None, created_at="", image_id="bad"
    )
    bad[field] = None
    good = SimpleNamespace(
        name="saved", url="registry/saved:v1", version="v1", created_at="2026", image_id="found"
    )
    api = SimpleNamespace(list_images_by_source=Mock(return_value=[SimpleNamespace(**bad), good]))
    assert (
        resolve_saved_image_id(
            {}, name="saved", version="v1", workspace_id="ws-test", session=object(), api=api
        )
        == "found"
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "UNKNOWN"),
        ("", "UNKNOWN"),
        ("   ", "UNKNOWN"),
        ("rUnNiNg", "RUNNING"),
        ("job_running", "JOB_RUNNING"),
        ("job_creating", "JOB_CREATING"),
        ("Stopped", "STOPPED"),
        ("Running job-12345678-1234-1234-1234-123456789abc", "RUNNING"),
        ("https://example.invalid/private/token", "UNKNOWN"),
        ("/private/runtime/token", "UNKNOWN"),
        (r"C:\private\runtime", "UNKNOWN"),
        ("Unrecognised", "UNRECOGNISED"),
    ],
)
def test_ad1_all_five_workloads_share_public_status_contract(raw, expected, capsys):
    """One contract exercises both projections and the human list/detail paths."""
    for kind in ("hpc", "ray", "job", "serving", "notebook"):
        output = importlib.import_module(f"inspire.services.{kind}.{kind}_output")
        row = {"name": "demo", "status": raw, "created_at": ""}
        wanted = expected
        if kind == "job":
            wanted = {
                "JOB_RUNNING": "RUNNING", "JOB_CREATING": "PENDING",
                "STOPPED": "CANCELLED", "UNRECOGNISED": "UNKNOWN",
            }.get(expected, expected)
        project_list = getattr(output, f"public_{kind}_list_item")
        project_detail = getattr(output, f"public_{kind}" if kind in ("serving", "notebook")
                                 else f"public_{kind}_status")
        assert project_list(row)["status"] == wanted, kind
        detail = project_detail(row)
        assert detail["status"] == wanted, kind
        if kind == "notebook":
            from inspire.cli.commands.notebook import notebook_presenters as presenter
            presenter._print_notebook_list([row], False)
            assert wanted in capsys.readouterr().out
            presenter._print_notebook_detail(detail)
            assert f"Status: {wanted}" in capsys.readouterr().out
        elif kind == "job":
            from inspire.cli.commands.job.job_commands import _format_job_list
            from inspire.cli.commands.job.public_output import format_job_status
            assert wanted in _format_job_list([row])
            assert f"Status: {wanted}" in format_job_status(detail)
        elif kind in ("hpc", "ray"):
            assert f"Status: {wanted}" in getattr(output, f"format_{kind}_status")(detail)
        else:
            from inspire.cli.commands.serving.serving_commands import _format_list_rows
            assert wanted in _format_list_rows([project_list(row)], 1)


@pytest.mark.parametrize("failure", ["duplicate", "empty", "limit", "missing_id"])
def test_ad2_incomplete_model_catalogue_has_actionable_error(monkeypatch, failure):
    from inspire.config import ConfigError
    from inspire.services.serving import serving_submission as service

    def page(**kwargs):
        number = kwargs["page"]
        if failure == "empty":
            return [], 2
        key = "" if failure == "missing_id" else str(number if failure == "limit" else 1)
        return [SimpleNamespace(model_id=key)], 10001

    listing = Mock(side_effect=page)
    resolver = Mock(side_effect=AssertionError("Must not resolve an incomplete catalogue"))
    monkeypatch.setattr(service.browser_api_module, "list_models", listing)
    with pytest.raises(ConfigError) as error:
        service.resolve_model_for_create(
            name="demo", workspace_id="ws-test", project_id=None,
            user_id="user-test", session=object(), resolve=resolver,
        )
    message = str(error.value)
    assert "Could not finish model lookup" in message
    assert "serving create" in message
    assert "Model catalog enumeration is incomplete." not in message
    if failure == "limit":
        assert listing.call_count == 100
        assert "more specific --model name" in message
    else:
        assert "platform administrator" in message
        assert listing.call_count == (2 if failure == "duplicate" else 1)
    resolver.assert_not_called()


def test_ad2_exact_match_does_not_hide_later_same_name(monkeypatch):
    from inspire.platform.web.browser_api.models import ModelInfo
    from inspire.services.serving import serving_submission as service

    first = ModelInfo(model_id="model-first", name="demo", latest_version="1")
    second = ModelInfo(model_id="model-second", name="demo", latest_version="2")
    listing = Mock(side_effect=[([first], 2), ([second], 2)])
    monkeypatch.setattr(service.browser_api_module, "list_models", listing)

    def resolve(candidates):
        assert [item["id"] for item in candidates] == ["model-first", "model-second"]
        return candidates[1]["id"]

    session = object()
    result = service.resolve_model_for_create(
        name="demo", workspace_id="ws-test", project_id="project-test",
        user_id="user-test", session=session, resolve=resolve,
    )
    assert result == ("model-second", 2, "demo")
    assert listing.call_count == 2
    for page, call in enumerate(listing.call_args_list, 1):
        assert call.kwargs == dict(
            workspace_id="ws-test", keyword="demo", project_ids=["project-test"],
            user_id="user-test", page=page, page_size=100, session=session,
        )
