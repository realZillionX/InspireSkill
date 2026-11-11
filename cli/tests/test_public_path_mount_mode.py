"""Independent console contract, exercised through real creators and offline catalogs."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import importlib
import json
from pathlib import Path
import warnings

import pytest
from click.testing import CliRunner

from inspire import InspireAsyncClient, PublicPathMountMode, ValidationError
from inspire.cli.main import main
from inspire.platform.web import browser_api as api
from test_sdk import client as client, planned as job_catalog  # noqa: F401
from test_sdk_notebooks import catalog as notebook_catalog  # noqa: F401
from test_sdk_hpc import catalog as base_catalog  # noqa: F401
from test_sdk_servings import catalog as serving_catalog  # noqa: F401
from test_dry_run_and_batch import _patch_submit_deps, FakeWebSession

CONTRACT = json.loads(
    (Path(__file__).parent / "fixtures/public_path_mount_contract.json").read_text()
)
CASES = CONTRACT["cases"]
KINDS = list(CONTRACT["resources"])
FACADES = {
    "job": "jobs",
    "notebook": "notebooks",
    "hpc": "hpc",
    "ray": "ray",
    "serving": "servings",
}


def assert_wire(body, case):
    assert CONTRACT["forbidden_create_field"] not in body
    assert {k: v for k, v in body.items() if k == CONTRACT["wire_field"]} == case["wire"]
    if CONTRACT["wire_field"] in body:
        assert type(body[CONTRACT["wire_field"]]) is int


def capture_writes(monkeypatch, kind):
    calls = []

    def request(*args, **kwargs):
        body = kwargs.get("body")
        if "Action=Create" in args[2]:
            route, action = CONTRACT["resources"][kind]["action"].split(".")
            assert args[2] == f"/api/v2/{route}?Action={action}"
            calls.append(body)
        return {
            "Result": {
                "id": "created",
                "job_id": "created",
                "ray_job_id": "created",
                "notebook_id": "created",
                "inference_serving_id": "created",
            }
        }

    module = importlib.import_module(
        "inspire.platform.web.browser_api."
        + {
            "job": "jobs",
            "hpc": "hpc_jobs",
            "ray": "ray_jobs",
            "notebook": "notebooks",
            "serving": "servings",
        }[kind]
    )
    monkeypatch.setattr(module, "_request_json", request)
    return calls


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("case", CASES, ids=lambda x: x["name"])
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_sdk_create_contract(client, request, monkeypatch, kind, case, asynchronous):
    if kind == "job":
        spec = request.getfixturevalue("job_catalog")
    elif kind == "notebook":
        spec = request.getfixturevalue("notebook_catalog").spec
    else:
        catalog = request.getfixturevalue(
            "serving_catalog" if kind == "serving" else "base_catalog"
        )
        spec = getattr(catalog, kind)
    spec = replace(spec, **case["options"])
    calls = capture_writes(monkeypatch, kind)
    facade = FACADES[kind]

    async def run():
        async with InspireAsyncClient("alpha") as c:
            # Preserve the fake catalog bindings while exercising the real async
            # facade, greenlet driver and create implementation.
            monkeypatch.setattr(c, "_operation_client", lambda: client)
            return await getattr(c, facade).create(spec)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        if "error" in case:
            with pytest.raises(ValidationError, match=case["error"]):
                asyncio.run(run()) if asynchronous else getattr(client, facade).create(spec)
            assert calls == []
        else:
            asyncio.run(run()) if asynchronous else getattr(client, facade).create(spec)
            assert len(calls) == 1
            assert_wire(calls[0], case)
    if "public_path_readonly" in case["options"]:
        assert any(issubclass(w.category, DeprecationWarning) for w in caught)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("case", CASES, ids=lambda x: x["name"])
@pytest.mark.parametrize("entry", ["batch", "create"])
def test_cli_contract(monkeypatch, tmp_path, kind, case, entry):
    _patch_submit_deps(monkeypatch, tmp_path)
    batch = importlib.import_module("inspire.cli.commands.batch")
    monkeypatch.setattr(batch, "get_web_session", lambda: FakeWebSession())
    serving = importlib.import_module("inspire.cli.commands.serving.serving_commands")
    monkeypatch.setattr(
        serving, "_resolve_model_for_create", lambda **kw: ("model-test", 1, "Model")
    )
    monkeypatch.setattr(
        serving, "_resolve_image_for_create", lambda *a, **kw: ("image-test", "Image")
    )
    monkeypatch.setattr(serving, "_resolve_project_id", lambda **kw: "project-test")
    monkeypatch.setattr(serving, "_resolve_image_id", lambda *a, **kw: "image-test")
    monkeypatch.setattr(api, "notebook_name_exists", lambda *a, **kw: False)
    monkeypatch.setattr(api, "get_current_user", lambda **kw: {"id": "user-test"})
    ray = importlib.import_module("inspire.services.ray.ray_submission")
    monkeypatch.setattr(ray, "resolve_image_id", lambda *a, **kw: "image-test")
    for resource in ("serving", "ray", "hpc"):
        command = importlib.import_module(f"inspire.cli.commands.{resource}.{resource}_commands")
        monkeypatch.setattr(command, "get_web_session", lambda: FakeWebSession())
    ray_command = importlib.import_module("inspire.cli.commands.ray.ray_commands")
    monkeypatch.setattr(ray_command, "_resolve_image_id", lambda *a, **kw: "image-test")
    flow = importlib.import_module("inspire.cli.commands.notebook.notebook_create_flow")
    quota_module = importlib.import_module("inspire.cli.utils.quota_resolver")
    monkeypatch.setattr(flow, "resolve_quota", quota_module.resolve_quota)
    monkeypatch.setattr(flow, "require_web_session", lambda *a, **kw: FakeWebSession())
    monkeypatch.setattr(flow, "remember_resource_identity", lambda **kw: None)
    calls = []

    def create(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("payload", kwargs))
        return {
            "id": "created",
            "job_id": "created",
            "ray_job_id": "created",
            "notebook_id": "created",
        }

    monkeypatch.setattr(
        api,
        {
            "job": "create_training_job",
            "hpc": "create_hpc_job",
            "ray": "create_ray_job",
            "notebook": "create_notebook",
            "serving": "create_serving",
        }[kind],
        create,
    )
    if kind in ("notebook", "serving"):
        module = importlib.import_module(
            "inspire.platform.web.browser_api."
            + ("notebooks" if kind == "notebook" else "servings")
        )
        monkeypatch.setattr(api, "create_" + kind, getattr(module, "create_" + kind))
        calls = capture_writes(monkeypatch, kind)
    item = dict(
        type=kind,
        name="example",
        command="echo test",
        entrypoint="echo test",
        workspace="cpu",
        project="Project One",
        group="H200 Room",
        quota="0,20,80",
        image="registry.batch/notebook:latest",
        model="Model",
        model_version=1,
        port=8000,
        **case["options"],
    )
    path = tmp_path / "batch.json"
    if kind == "ray":
        item["workers"] = [
            "name=w;image=registry.batch/notebook:latest;group=H200 Room;quota=0,20,80;min=1;max=2"
        ]
    path.write_text(
        json.dumps({{"notebook": "notebooks", "serving": "servings"}.get(kind, "jobs"): [item]})
    )
    args = ["--json", kind, "batch", str(path)]
    if entry == "create":
        args = ["--json", kind, "create"]
        for key in ("name", "workspace", "project", "group", "quota", "image"):
            args.extend(["--" + key, str(item[key])])
        if kind == "notebook":
            args.append("--no-wait")
        if kind == "hpc":
            args.extend(["--entrypoint", item["entrypoint"]])
        elif kind != "notebook":
            args.extend(["--command", item["command"]])
        if kind == "serving":
            args.extend(["--model", "Model", "--model-version", "1", "--port", "8000"])
        if kind == "ray":
            args.extend(["--worker", item["workers"][0]])
        for key, value in case["options"].items():
            if key == "public_path_readonly":
                args.append("--public-path-readonly" if value else "--no-public-path-readonly")
            else:
                args.extend(["--public-path-mount-mode", str(value)])
    result = CliRunner().invoke(main, args)
    if "error" in case:
        assert result.exit_code != 0
        assert case["error"] in result.output
        assert calls == []
    else:
        assert result.exit_code == 0, result.output
        assert len(calls) == 1
        assert_wire(calls[0], case)
    if "public_path_readonly" in case["options"]:
        assert "Deprecated" in result.stderr


def test_public_enum_matches_independent_contract():
    assert {member.name: member.value for member in PublicPathMountMode} == CONTRACT["enum"]


@pytest.mark.parametrize(
    "view",
    [
        "inspire.services.job.job_output.public_job_status",
        "inspire.services.notebook.notebook_output.public_notebook",
        "inspire.services.serving.serving_output.public_serving",
    ],
)
@pytest.mark.parametrize(
    "mode,legacy,expected",
    [(None, False, 0), (None, True, 1), (0, True, 0), (1, False, 1), (2, True, 2)],
)
def test_detail_prefers_enum(view, mode, legacy, expected):
    module, function = view.rsplit(".", 1)
    value = getattr(importlib.import_module(module), function)(
        {"publicpath_mount_mode": mode, "is_publicpath_readonly": legacy}
    )
    assert value["public_path_mount_mode"] == expected


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_rejected_create_is_not_retried(client, request, monkeypatch, kind, asynchronous):
    if kind == "job":
        spec = request.getfixturevalue("job_catalog")
    elif kind == "notebook":
        spec = request.getfixturevalue("notebook_catalog").spec
    else:
        catalog = request.getfixturevalue(
            "serving_catalog" if kind == "serving" else "base_catalog"
        )
        spec = getattr(catalog, kind)
    spec = replace(spec, public_path_mount_mode=PublicPathMountMode.NOT_MOUNTED)
    module = importlib.import_module(
        "inspire.platform.web.browser_api."
        + {
            "job": "jobs",
            "hpc": "hpc_jobs",
            "ray": "ray_jobs",
            "notebook": "notebooks",
            "serving": "servings",
        }[kind]
    )
    calls = []

    def reject(*args, **kwargs):
        calls.append(kwargs["body"])
        raise ValueError("unknown field")

    monkeypatch.setattr(module, "_request_json", reject)

    async def run():
        async with InspireAsyncClient("alpha") as c:
            monkeypatch.setattr(c, "_operation_client", lambda: client)
            await getattr(c, FACADES[kind]).create(spec)

    with pytest.raises(ValidationError, match="unknown field"):
        asyncio.run(run()) if asynchronous else getattr(client, FACADES[kind]).create(spec)
    assert len(calls) == 1
    assert_wire(calls[0], {"wire": {"publicpath_mount_mode": 2}})


@pytest.mark.parametrize("mode", [-1, 3, True, False, 1.0, "1"])
def test_invalid_mode_is_not_coerced(mode):
    from inspire.services.public_path import public_path_mount_fields

    with pytest.raises(ValidationError, match="must be 0, 1, or 2"):
        public_path_mount_fields(mode)


@pytest.mark.parametrize("data", [[], "invalid"])
def test_batch_json_rejects_non_object_config(tmp_path, data):
    from inspire.config import ConfigError

    batch = importlib.import_module("inspire.cli.commands.batch")
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigError, match="Batch config must be an object at the top level"):
        batch._load_config(path)
