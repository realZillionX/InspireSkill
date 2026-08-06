from __future__ import annotations

import importlib
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from inspire.cli.commands.hpc import hpc_commands
from inspire.cli.commands.job import job_commands
from inspire.cli.commands.notebook import notebook_commands
from inspire.cli.commands.ray import ray_commands
from inspire.cli.commands.serving import serving_commands
from inspire.cli.context import Context
from inspire.cli.main import main as cli_main
from inspire.cli.utils.id_resolver import NAME_PICK_HELP
from inspire.config import ConfigError

notebook_cli_module = importlib.import_module("inspire.cli.utils.notebook_cli")


_COLLECTION_PATHS = (
    ("job", "list"),
    ("notebook", "list"),
    ("hpc", "list"),
    ("ray", "list"),
    ("serving", "list"),
    ("model", "list"),
    ("project", "list"),
    ("resources", "availability"),
    ("resources", "nodes"),
    ("serving", "configs"),
    ("user", "permissions"),
    ("job", "quota"),
    ("notebook", "quota"),
    ("hpc", "quota"),
    ("ray", "quota"),
    ("serving", "quota"),
)

_SINGLE_RESOURCE_PATHS = (
    ("job", "status"),
    ("job", "instances"),
    ("job", "stop"),
    ("job", "delete"),
    ("job", "wait"),
    ("job", "command"),
    ("job", "shell"),
    ("job", "events"),
    ("job", "logs"),
    ("job", "metrics"),
    ("notebook", "status"),
    ("notebook", "start"),
    ("notebook", "stop"),
    ("notebook", "delete"),
    ("notebook", "events"),
    ("notebook", "lifecycle"),
    ("notebook", "metrics"),
    ("notebook", "url"),
    ("notebook", "vscode"),
    ("notebook", "proxy-url"),
    ("hpc", "status"),
    ("hpc", "instances"),
    ("hpc", "stop"),
    ("hpc", "delete"),
    ("hpc", "events"),
    ("hpc", "metrics"),
    ("ray", "status"),
    ("ray", "instances"),
    ("ray", "stop"),
    ("ray", "delete"),
    ("ray", "events"),
    ("ray", "metrics"),
    ("serving", "status"),
    ("serving", "instances"),
    ("serving", "start"),
    ("serving", "stop"),
    ("serving", "delete"),
    ("serving", "events"),
    ("serving", "metrics"),
)


def _resolve_command(path: tuple[str, str]) -> click.Command:
    command: click.Command = cli_main
    for name in path:
        assert isinstance(command, click.Group)
        command = command.commands[name]
    return command


def _option(command: click.Command, name: str) -> click.Option:
    return next(
        parameter
        for parameter in command.params
        if isinstance(parameter, click.Option) and parameter.name == name
    )


@pytest.mark.parametrize("path", _COLLECTION_PATHS)
def test_workload_collections_accept_workspace_name_or_all(
    path: tuple[str, str],
) -> None:
    workspace = _option(_resolve_command(path), "workspace")

    assert workspace.required
    assert workspace.help == "Workspace name or 'all'."


@pytest.mark.parametrize("path", _SINGLE_RESOURCE_PATHS)
def test_single_workload_commands_share_name_and_workspace_contract(
    path: tuple[str, str],
) -> None:
    command = _resolve_command(path)
    arguments = [
        parameter
        for parameter in command.params
        if isinstance(parameter, click.Argument)
    ]
    workspace = _option(command, "workspace")
    pick = _option(command, "pick")

    assert arguments
    assert arguments[0].metavar == "NAME"
    assert workspace.required
    assert workspace.help == "Workspace name."
    assert isinstance(pick.type, click.IntRange)
    assert pick.type.min == 1
    assert pick.type.max is None
    assert pick.default is None
    assert pick.help == NAME_PICK_HELP


def test_job_single_workspace_resolver_rejects_all_before_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        job_commands,
        "get_web_session",
        lambda: (_ for _ in ()).throw(
            AssertionError("workspace validation must run before session setup")
        ),
    )

    with pytest.raises(
        ConfigError,
        match=r"--workspace must be a workspace name for this command\.",
    ):
        job_commands._resolve_web_job_id(
            job="train-a",
            workspace="all",
            all_workspaces=False,
            max_pages=1,
            workspace_must_be_single=True,
        )


@pytest.mark.parametrize(
    ("command", "extra_args"),
    (
        ("status", ()),
        ("events", ()),
        ("lifecycle", ()),
        ("metrics", ("--no-plot",)),
        ("url", ()),
        ("vscode", ()),
        ("proxy-url", ("--port", "30000")),
    ),
)
def test_notebook_single_resource_commands_reject_workspace_all(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    extra_args: tuple[str, ...],
) -> None:
    session = SimpleNamespace(
        all_workspace_ids=["ws-internal"],
        all_workspace_names={"ws-internal": "Training Room"},
        storage_state={},
    )
    config = SimpleNamespace()

    monkeypatch.setattr(
        notebook_cli_module,
        "require_web_session",
        lambda *_args, **_kwargs: session,
    )
    monkeypatch.setattr(
        notebook_cli_module,
        "load_config",
        lambda _ctx: config,
    )
    monkeypatch.setattr(
        notebook_cli_module,
        "get_base_url",
        lambda: "https://example.invalid",
    )
    monkeypatch.setattr(
        notebook_commands,
        "require_web_session",
        lambda *_args, **_kwargs: session,
    )
    monkeypatch.setattr(
        notebook_commands,
        "load_config",
        lambda _ctx: config,
    )
    monkeypatch.setattr(
        notebook_commands,
        "get_base_url",
        lambda: "https://example.invalid",
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "notebook",
            command,
            "demo",
            "--workspace",
            "all",
            *extra_args,
        ],
    )

    assert result.exit_code != 0
    assert "--workspace requires one workspace name for this command." in result.output
    assert "ws-internal" not in result.output


@pytest.mark.parametrize(
    "resolver",
    (
        lambda: hpc_commands._resolve_hpc_name_in_workspace(
            Context(),
            session=object(),
            name="prep-a",
            workspace="all",
            limit=1,
        ),
        lambda: ray_commands._resolve_ray_name_in_workspace(
            Context(),
            session=object(),
            name="ray-a",
            workspace="all",
            limit=1,
        ),
        lambda: serving_commands._resolve_workspace_id(
            "all",
            session=object(),
        ),
    ),
)
def test_existing_single_workspace_resolvers_reject_all(resolver) -> None:  # noqa: ANN001
    with pytest.raises(
        ConfigError,
        match=r"--workspace requires one workspace name for this command\.",
    ):
        resolver()
