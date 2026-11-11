"""Public-path mount behavior through real creators and offline catalogs."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import importlib
import json
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

CASES = [
    {"name": "omitted", "options": {}, "wire": {}},
    {
        "name": "legacy_false",
        "options": {"public_path_readonly": False},
        "wire": {"publicpath_mount_mode": 0},
    },
    {
        "name": "legacy_true",
        "options": {"public_path_readonly": True},
        "wire": {"publicpath_mount_mode": 1},
    },
    *[
        {
            "name": f"explicit_{mode}",
            "options": {"public_path_mount_mode": mode},
            "wire": {"publicpath_mount_mode": mode},
        }
        for mode in (0, 1, 2)
    ],
    {
        "name": "conflict",
        "options": {"public_path_mount_mode": 2, "public_path_readonly": False},
        "error": "conflicts",
    },
]
KINDS = ("notebook", "job", "hpc", "ray", "serving")
FACADES = {
    "job": "jobs",
    "notebook": "notebooks",
    "hpc": "hpc",
    "ray": "ray",
    "serving": "servings",
}


def assert_wire(body, case):
    assert "is_publicpath_readonly" not in body
    assert {k: v for k, v in body.items() if k == "publicpath_mount_mode"} == case["wire"]
    if "publicpath_mount_mode" in body:
        assert type(body["publicpath_mount_mode"]) is int


def capture_writes(monkeypatch, kind):
    calls = []

    def request(*args, **kwargs):
        body = kwargs.get("body")
        if "Action=Create" in args[2]:
            assert "is_publicpath_readonly" not in body
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
@pytest.mark.parametrize(
    "case,asynchronous",
    [
        pytest.param(case, asynchronous, id=f"{'async' if asynchronous else 'sync'}-{case['name']}")
        for asynchronous in (False, True)
        for case in CASES
        # The async facade shares validation and payload construction with sync.
        if not asynchronous or case["name"] in ("omitted", "legacy_true", "explicit_2", "conflict")
    ],
)
def test_sdk_create_mount_mode(client, request, monkeypatch, kind, case, asynchronous):
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
    assert len(caught) == int("public_path_readonly" in case["options"])
    assert all(issubclass(w.category, DeprecationWarning) for w in caught)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("case", CASES, ids=lambda x: x["name"])
@pytest.mark.parametrize(
    "entry,file_format", [("create", "json"), ("batch", "json"), ("batch", "toml")]
)
def test_cli_create_mount_mode(monkeypatch, tmp_path, kind, case, entry, file_format):
    check_cli_mount_mode(monkeypatch, tmp_path, kind, case, entry, file_format=file_format)


def check_cli_mount_mode(
    monkeypatch, tmp_path, kind, case, entry, dry_run=False, file_format="json"
):
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
    # Capture at the transport boundary so every CLI path uses its real builder.
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
    creator = {
        "job": "create_training_job",
        "hpc": "create_hpc_job",
        "ray": "create_ray_job",
        "notebook": "create_notebook",
        "serving": "create_serving",
    }[kind]
    monkeypatch.setattr(api, creator, getattr(module, creator))
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
    path = tmp_path / f"batch.{file_format}"
    if kind == "ray":
        item["workers"] = [
            "name=w;image=registry.batch/notebook:latest;group=H200 Room;quota=0,20,80;min=1;max=2"
        ]
    item_key = {"notebook": "notebooks", "serving": "servings"}.get(kind, "jobs")
    if file_format == "toml":
        path.write_text(
            f"[[{item_key}]]\n"
            + "\n".join(f"{key} = {json.dumps(value)}" for key, value in item.items())
        )
    else:
        path.write_text(json.dumps({item_key: [item]}))
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
            elif isinstance(value, bool):
                flag = key.replace("_", "-")
                args.append("--" + ("" if value else "no-") + flag)
            else:
                flag = {
                    "keep_after_finish_hours": "keep-after-finish",
                    "keep_after_success_hours": "keep-after-success",
                    "keep_after_failure_hours": "keep-after-failure",
                }.get(key, key.replace("_", "-"))
                args.extend(["--" + flag, str(value)])
    if dry_run:
        args.append("--dry-run")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        result = CliRunner().invoke(main, args)
    assert len(caught) == int("public_path_readonly" in case["options"])
    assert all(issubclass(w.category, DeprecationWarning) for w in caught)
    if "error" in case:
        assert result.exit_code != 0
        assert case["error"] in result.output
        assert calls == []
    elif dry_run:
        assert result.exit_code == 0, result.output
        assert calls == []
        output = json.loads(result.stdout)["data"]["items"][0]
        expected = (
            {"public_path_mount_mode": case["wire"]["publicpath_mount_mode"]}
            if case["wire"]
            else {}
        )
        assert {k: v for k, v in output.items() if k == "public_path_mount_mode"} == expected
        assert "public_path_readonly" not in output
    else:
        assert result.exit_code == 0, result.output
        assert len(calls) == 1
        assert_wire(calls[0], case)
    if "public_path_readonly" in case["options"]:
        assert result.stderr.count("Deprecated") == 1
    else:
        assert "Deprecated" not in result.stderr


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("case", [CASES[0], CASES[2], CASES[5]], ids=lambda x: x["name"])
@pytest.mark.parametrize("file_format", ["json", "toml"])
def test_batch_dry_run_mount_mode(monkeypatch, tmp_path, kind, case, file_format):
    check_cli_mount_mode(monkeypatch, tmp_path, kind, case, "batch", True, file_format)


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
