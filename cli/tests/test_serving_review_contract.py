"""PR #114 review regressions at SDK and CLI boundaries."""

from dataclasses import replace
import json

from click.testing import CliRunner
import pytest

from test_sdk_servings import client as client, catalog as catalog
from test_sdk_servings import base_catalog as base_catalog  # noqa: F401
from test_serving_commands import _patch_serving_cli_deps
from inspire import ResourceNotFoundError, ServingRef
from inspire.cli.commands.serving import serving_commands as cli
from inspire.cli.main import main
from inspire.platform.web import browser_api as api
from inspire.platform.web.browser_api import servings as browser
from inspire.services.serving.serving_output import public_serving
from inspire.services.serving.serving_views import public_serving_version


HTTP = {"port": 30000, "type": "PORT_TYPE_HTTP", "path": "/"}
METRICS = {"port": 30001, "type": "PORT_TYPE_METRICS", "path": "/metrics"}


@pytest.mark.parametrize("via", ["sdk", "cli"])
def test_versions_prefer_current_ports_and_preserve_legacy_rows(client, monkeypatch, via):
    rows = [
        {"version": 2, "port": 0, "port_configs": [HTTP, METRICS]},
        {"version": 1, "port": "8080"},
    ]
    monkeypatch.setattr(api, "list_serving_versions", lambda *a, **kw: (rows, 2))
    if via == "sdk":
        ref = ServingRef("example", client.account, client.base_url, "serving-test", "ws-test")
        versions = client.servings.versions(ref)
        assert versions[0].port == 30000 and versions[0].metrics_port == 30001
        result = [row.to_dict() for row in versions]
    else:
        _patch_serving_cli_deps(monkeypatch, resolved_id="serving-test")
        output = CliRunner().invoke(main, ["--json", "serving", "versions", "example",
                                          "--workspace", "Workspace"])
        assert output.exit_code == 0, output.output
        result = json.loads(output.stdout)["data"]["items"]
        human = CliRunner().invoke(main, ["serving", "versions", "example",
                                         "--workspace", "Workspace"])
        assert human.exit_code == 0, human.output
        assert all(value in human.output for value in ("Port", "30000", "30001", "8080"))
    assert result == [{"version": 2, "port": 30000, "metrics_port": 30001},
                      {"version": 1, "port": 8080}]


@pytest.mark.parametrize("configs,expected", [
    ([HTTP], {"port": 30000}),
    ([METRICS], {"port": 8080, "metrics_port": 30001}),
    ([dict(HTTP, port="30000")], {"port": 30000}),
    ([], {"port": 8080}),
    ([None, dict(HTTP, port=None)], {"port": 8080}),
])
def test_detail_and_versions_share_port_projection(configs, expected):
    row = {"port": 8080, "port_configs": configs}
    detail = public_serving(row)
    version = public_serving_version(row)
    assert {key: detail[key] for key in expected} == expected
    assert version == expected


@pytest.mark.parametrize("source", [None, "registered", "plaza", "MODEL_PLAZA"])
def test_sdk_source_aliases_send_only_platform_fields(client, catalog, monkeypatch, source):
    plaza = source in ("plaza", "MODEL_PLAZA")
    monkeypatch.setattr(api, "list_plaza_models", lambda **kw: ([{
        "name": "Model", "model_id": "model-test", "status": "PUBLISHED",
    }], 1))
    calls = []
    monkeypatch.setattr(browser, "_request_json", lambda *a, **kw:
                        calls.append(kw["body"]) or {"Result": {"inference_serving_id": "s"}})
    spec = replace(catalog.serving, model_source=source)
    plan = client.servings.plan(spec)
    assert plan.create_kwargs["model_source"] == ("MODEL_PLAZA" if plaza else None)
    assert plan.model_version == (None if plaza else 3)
    assert client.servings.create(spec).ref.key == "s"
    assert len(calls) == 1
    if plaza:
        assert calls[0]["model_source"] == "MODEL_PLAZA" and "model_version" not in calls[0]
    else:
        assert "model_source" not in calls[0] and calls[0]["model_version"] == 3
    assert spec.model_source == source


@pytest.mark.parametrize("via", ["sdk", "cli"])
@pytest.mark.parametrize("name,expected", [("Model", "upper"), ("model", "lower"),
                                          (" Model ", "upper"), ("MODEL", None)])
def test_plaza_names_use_same_case_sensitive_rule_after_paging(
    client, catalog, monkeypatch, via, name, expected,
):
    pages = []

    def listing(**kwargs):
        page = kwargs["page"]
        pages.append(page)
        return [{"model_id": "upper" if page == 1 else "lower",
                 "name": "Model" if page == 1 else "model", "status": "PUBLISHED"}], 2

    monkeypatch.setattr(api, "list_plaza_models", listing)
    monkeypatch.setattr(api, "create_serving", lambda **kw: pytest.fail("must not create"))
    if via == "sdk":
        spec = replace(catalog.serving, model=name, model_source="MODEL_PLAZA")
        if expected is None:
            with pytest.raises(ResourceNotFoundError):
                client.servings.plan(spec)
        else:
            assert client.servings.plan(spec).create_kwargs["model_id"] == expected
    else:
        from inspire.cli.utils import quota_resolver

        monkeypatch.setattr(cli, "get_web_session", lambda: client._transport._session)
        monkeypatch.setattr(cli, "select_workspace_id", lambda **kw: "ws-test")
        monkeypatch.setattr(cli, "workspace_label", lambda *a: "Workspace")
        monkeypatch.setattr(cli, "_resolve_project_id", lambda **kw: "project-test")
        monkeypatch.setattr(quota_resolver, "resolve_quota", lambda **kw: catalog.resolved)
        result = CliRunner().invoke(main, ["--json", "serving", "create", "--dry-run",
            "--name", "example", "--model", name, "--model-source", "plaza",
            "--command", "python serve.py", "--port", "8000", "--workspace", "Workspace",
            "--project", "Project", "--group", "Group", "--quota", "0,8,32", "--image", "Image"])
        if expected is None:
            assert result.exit_code != 0
            assert not result.stdout
            assert json.loads(result.stderr)["success"] is False
        else:
            assert result.exit_code == 0, result.output
            assert json.loads(result.stdout)["data"]["model"] == name.strip()
    assert pages == [1, 2]


def test_registered_sdk_name_matching_remains_case_insensitive(client, catalog):
    assert client.servings.plan(replace(catalog.serving, model="MODEL")).model == "Model"
