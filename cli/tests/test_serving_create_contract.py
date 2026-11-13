"""Console request contracts from issue #113, without network or allocations."""

from dataclasses import replace
import json

from click.testing import CliRunner
import pytest

from test_sdk_servings import client as client, catalog as catalog
from test_sdk_servings import base_catalog as base_catalog  # noqa: F401
from inspire import AmbiguousResourceError, ValidationError
from inspire.cli.commands.serving import serving_commands as cli
from inspire.cli.main import main
from inspire.platform.web import browser_api as api
from inspire.platform.web.browser_api import servings as browser
from inspire.services.serving.serving_contract import normalize_port_configs
from inspire.services.serving.serving_output import public_serving


HTTP = {"port": 30000, "type": "PORT_TYPE_HTTP", "path": "/"}
METRICS = {"port": 30001, "type": "PORT_TYPE_METRICS", "path": "/metrics"}


@pytest.mark.parametrize("via", ["sdk", "cli", "batch"])
@pytest.mark.parametrize("plaza", [False, True])
def test_sdk_and_cli_send_console_body(client, catalog, monkeypatch, tmp_path, via, plaza):
    """Check the final wire body, including fields that previously got dropped."""
    from inspire.cli.utils import quota_resolver

    catalog.price.update(gpu_count=8, cpu_count=64, memory_size_gib=256,
                         gpu_info={"gpu_type": "test-gpu"})
    quota = replace(catalog.resolved, gpu_count=8, cpu_count=64, memory_gib=256)
    calls = []

    def request(session, method, url, *, body, **kwargs):
        assert session is client._transport._session
        assert method == "POST"
        calls.append((url, body))
        if url.endswith("Action=GetModelListInPlaza"):
            assert body == {"page": 1, "page_size": 100, "filter": {"workspace_id": "ws-test"}}
            return {"Result": {"items": [{
                "model_id": "model-test", "name": "Model", "status": "PUBLISHED", "version": 0,
            }], "total_count": "1"}}
        assert url.endswith("/api/v2/inference_serving?Action=CreateServingConsole")
        return {"Result": {"inference_serving_id": "serving-test",
                           "extra_info": {"service": "https://example.invalid"}}}

    monkeypatch.setattr(browser, "_request_json", request)
    if plaza:
        monkeypatch.setattr(api, "list_models", lambda **kw: pytest.fail("plaza is not registry"))
    monkeypatch.setattr(cli, "get_web_session", lambda: client._transport._session)
    monkeypatch.setattr(cli, "select_workspace_id", lambda **kw: "ws-test")
    monkeypatch.setattr(cli, "workspace_label", lambda *a: "Workspace")
    monkeypatch.setattr(cli, "_resolve_project_id", lambda **kw: "project-test")
    monkeypatch.setattr(quota_resolver, "resolve_quota", lambda **kw: quota)

    spec = replace(catalog.serving, port=30000, port_configs=[HTTP, METRICS],
                   model_source="MODEL_PLAZA" if plaza else None,
                   model_version=None if plaza else 2, quota="8,64,256", priority=4,
                   shm_gib=64, auto_scaling=False, public_path_mount_mode=1)
    if via == "sdk":
        plan = client.servings.plan(spec)
        assert plan.model_version == (None if plaza else 2)
        assert plan.to_dict()["port_configs"] == [HTTP, METRICS]
        calls.clear()
        assert client.servings.create(spec).ref.key == "serving-test"
    elif via == "cli":
        args = ["--json", "serving", "create", "--name", "example", "--model", "Model",
                "--command", "python serve.py", "--port", "30000", "--metrics-port", "30001",
                "--workspace", "Workspace", "--project", "Project", "--group", "Group",
                "--quota", "8,64,256", "--image", "Image", "--priority", "4",
                "--shm-size", "64", "--no-auto-scaling", "--public-path-mount-mode", "1"]
        args += ["--model-source", "plaza"] if plaza else ["--model-version", "2"]
        dry_run = CliRunner().invoke(main, [*args, "--dry-run"])
        assert dry_run.exit_code == 0, dry_run.output
        assert not any(url.endswith("Action=CreateServingConsole") for url, _ in calls)
        assert json.loads(dry_run.stdout)["data"] == client.servings.plan(spec).to_dict()
        calls.clear()
        result = CliRunner().invoke(main, args)
        assert result.exit_code == 0, result.output
        assert "model-test" not in result.output and "serving-test" not in result.output
    else:
        from inspire.cli.commands import batch

        monkeypatch.setattr(batch, "get_web_session", lambda: client._transport._session)
        monkeypatch.setattr(batch, "select_workspace_id", lambda **kw: "ws-test")
        monkeypatch.setattr(batch, "resolve_quota", lambda **kw: quota)
        item = dict(name="example", model="Model", command="python serve.py", port=30000,
                    metrics_port=30001, workspace="Workspace", project="Project", group="Group",
                    quota="8,64,256", image="Image", priority=4, shm_size=64,
                    auto_scaling=False, public_path_mount_mode=1)
        item.update({"model_source": "plaza"} if plaza else {"model_version": 2})
        path = tmp_path / "servings.json"
        path.write_text(json.dumps({"servings": [item]}))
        args = ["--json", "serving", "batch", str(path)]
        dry_run = CliRunner().invoke(main, [*args, "--dry-run"])
        assert dry_run.exit_code == 0, dry_run.output
        assert not any(url.endswith("Action=CreateServingConsole") for url, _ in calls)
        assert json.loads(dry_run.stdout)["data"]["items"][0]["metrics_port"] == 30001
        calls.clear()
        result = CliRunner().invoke(main, args)
        assert result.exit_code == 0, result.output
        assert "model-test" not in result.output and "serving-test" not in result.output

    writes = [body for url, body in calls if url.endswith("Action=CreateServingConsole")]
    assert len(writes) == 1
    assert writes[0] == {
        "workspace_id": "ws-test", "project_id": "project-test",
        "inference_serving_type": "CUSTOM", "name": "example",
        "logic_compute_group_id": "group-test", "model_id": "model-test",
        **({"model_source": "MODEL_PLAZA"} if plaza else {"model_version": 2}),
        "mirror_id": "image-test", "command": "python serve.py",
        "port_configs": [HTTP, METRICS], "description": "", "replicas": 1,
        "node_num_per_replica": 1, "task_priority": 4, "shm_gi": 64,
        "publicpath_mount_mode": 1, "enable_auto_scaling": False,
        "resource_spec_price": {"cpu_type": "cpu", "cpu_count": 64, "gpu_type": "test-gpu",
                                "gpu_count": 8, "memory_size_gib": 256,
                                "logic_compute_group_id": "group-test", "quota_id": "quota-test"},
    }


@pytest.mark.parametrize("port,configs", [
    (None, None), (0, None), (65536, None), (True, None), (1.5, None), ("30000", None),
    (None, []), (None, {}), (None, [METRICS]), (None, [HTTP, HTTP]),
    (8080, [HTTP]), (None, [{**HTTP, "type": "unknown"}]),
    (None, [{**HTTP, "path": "/unsupported"}]), (None, [{**METRICS, "path": "/"}, HTTP]),
    (None, [{**HTTP, "port": False}]), (None, [{**HTTP, "port": 65536}]),
    (None, [{**HTTP, "extra": "not-supported"}]), (None, ["invalid"]),
])
def test_invalid_port_contract_fails_before_session_or_send(monkeypatch, port, configs):
    monkeypatch.setattr(browser, "get_web_session", lambda: pytest.fail("session acquired"))
    with pytest.raises(ValueError):
        browser.create_serving(workspace_id="w", project_id="p", name="example",
            logic_compute_group_id="g", model_id="m", model_version=1, mirror_id="i",
            command="python serve.py", port=port, port_configs=configs,
            resource_spec_price={}, task_priority=4)


def test_port_shorthand_copy_and_shared_metrics_port():
    assert normalize_port_configs(30000) == [HTTP]
    ports = [dict(METRICS, port=30000), dict(HTTP)]
    normalized = normalize_port_configs(30000, ports)
    assert normalized == [HTTP, dict(METRICS, port=30000)]
    normalized[0]["port"] = 1
    assert ports[1]["port"] == 30000


@pytest.mark.parametrize("source,version", [
    ("MODEL_PLAZA", 1), ("MODEL_PLAZA", 0), ("MODEL_PLAZA", "1"),
    (None, None), (None, True), (None, 0), (None, 1.5), ("unknown", 1),
])
def test_invalid_model_contract_fails_before_session(monkeypatch, source, version):
    monkeypatch.setattr(browser, "get_web_session", lambda: pytest.fail("session acquired"))
    with pytest.raises(ValueError):
        browser.create_serving(workspace_id="w", project_id="p", name="example",
            logic_compute_group_id="g", model_id="m", model_version=version, model_source=source,
            mirror_id="i", command="python serve.py", port=30000,
            resource_spec_price={}, task_priority=4)


def test_plaza_low_level_omits_version_and_accepts_configs_without_port(monkeypatch):
    captured = []
    monkeypatch.setattr(browser, "_request_json", lambda *a, **kw:
                        captured.append(kw["body"]) or {"Result": {"id": "example"}})
    browser.create_serving(workspace_id="w", project_id="p", name="example",
        logic_compute_group_id="g", model_id="m", model_source="MODEL_PLAZA",
        mirror_id="i", command="python serve.py", port_configs=[HTTP],
        resource_spec_price={}, task_priority=4, session=object())
    assert "model_version" not in captured[0] and "port" not in captured[0]
    assert captured[0]["port_configs"] == [HTTP]


def test_plaza_sdk_configs_without_legacy_port(client, catalog, monkeypatch):
    monkeypatch.setattr(api, "list_plaza_models", lambda **kw: ([{
        "model_id": "m", "name": "Model", "status": "PUBLISHED", "version": 0,
    }], 1))
    plan = client.servings.plan(replace(catalog.serving, port=None, port_configs=[HTTP],
                                       model_source="MODEL_PLAZA"))
    assert plan.to_dict()["port"] == 30000 and plan.model_version is None


@pytest.mark.parametrize("outcome", ["ok", "timeout", "rejected"])
def test_plaza_create_dispatches_once_and_retains_failure_code(client, catalog, monkeypatch, outcome):
    import requests
    from inspire import SubmissionUncertainError

    monkeypatch.setattr(api, "list_plaza_models", lambda **kw: ([{
        "model_id": "m", "name": "Model", "status": "PUBLISHED",
    }], 1))
    calls = []

    def once(method, path, body, *args, **kwargs):
        calls.append(body)
        assert path.endswith("Action=CreateServingConsole")
        client._transport._write["sent"] = True
        if outcome == "timeout":
            raise requests.ReadTimeout("lost response")
        if outcome == "rejected":
            return {"ResponseMetadata": {"Error": {"Code": "InvalidParameter", "Message": "invalid"}}}
        return {"Result": {"inference_serving_id": "s"}}

    monkeypatch.setattr(client._transport, "_once", once)
    monkeypatch.setattr(client._transport, "_refresh", lambda: pytest.fail("must not replay write"))
    spec = replace(catalog.serving, model_source="MODEL_PLAZA")
    if outcome == "ok":
        assert client.servings.create(spec).ref.key == "s"
    else:
        expected = SubmissionUncertainError if outcome == "timeout" else ValidationError
        with pytest.raises(expected) as failure:
            client.servings.create(spec)
        if outcome == "rejected":
            assert "InvalidParameter" in str(failure.value)
    assert len(calls) == 1
    assert "model_version" not in calls[0] and "port" not in calls[0]


@pytest.mark.parametrize("broken", ["duplicate", "empty", "ambiguous", "unpublished"])
def test_plaza_resolution_completes_catalogue_before_write(client, catalog, monkeypatch, broken):
    pages = []

    def listing(**kwargs):
        pages.append(kwargs["page"])
        row = {"model_id": "m1", "name": "Model", "status": "PUBLISHED"}
        if kwargs["page"] == 1:
            if broken == "unpublished":
                row["status"] = "DRAFT"
                return [row], 1
            return [row], 2
        if broken == "empty":
            return [], 2
        if broken == "ambiguous":
            row["model_id"] = "m2"
        return [row], 2

    monkeypatch.setattr(api, "list_plaza_models", listing)
    monkeypatch.setattr(api, "create_serving", lambda **kw: pytest.fail("must not create"))
    from inspire import ResourceNotFoundError
    from inspire.config import ConfigError

    expected = {"duplicate": ConfigError, "empty": ConfigError,
                "ambiguous": AmbiguousResourceError, "unpublished": ResourceNotFoundError}[broken]
    with pytest.raises(expected):
        client.servings.create(replace(catalog.serving, model_source="MODEL_PLAZA"))
    assert pages == ([1] if broken == "unpublished" else [1, 2])


@pytest.mark.parametrize("data", [{}, {"items": [], "total_count": -1},
                                  {"items": [None], "total_count": 1}])
def test_malformed_plaza_response_is_not_an_empty_catalogue(monkeypatch, data):
    monkeypatch.setattr(browser, "_request_json", lambda *a, **kw: {"Result": data})
    with pytest.raises(ValueError, match="Incomplete model plaza response"):
        browser.list_plaza_models(workspace_id="w", session=object())


def test_sdk_and_cli_reject_plaza_version_before_lookup(client, catalog, monkeypatch):
    monkeypatch.setattr(client.workspaces, "get", lambda *a: pytest.fail("no lookup"))
    with pytest.raises(ValidationError, match="Omit model_version"):
        client.servings.plan(replace(catalog.serving, model_source="MODEL_PLAZA", model_version=1))
    result = CliRunner().invoke(main, ["serving", "create", "--name", "example", "--model", "Model",
        "--command", "python serve.py", "--port", "30000", "--workspace", "Workspace",
        "--project", "Project", "--group", "Group", "--quota", "8,64,256", "--image", "Image",
        "--model-source", "plaza", "--model-version", "1"])
    assert result.exit_code == 2
    assert "Omit --model-version" in result.output


@pytest.mark.parametrize("wrapped", [False, True])
def test_status_reads_current_ports_instead_of_zero_legacy_field(wrapped):
    data = {"name": "example", "status": "RUNNING", "port": 0, "port_configs": [HTTP, METRICS]}
    value = browser.ServingInfo("s", "example", raw=data) if wrapped else data
    view = public_serving(value)
    assert view["port"] == 30000 and view["metrics_port"] == 30001
