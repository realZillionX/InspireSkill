from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from inspire.cli.commands.hpc import hpc_commands, hpc_events
from inspire.cli.commands.job import job_commands, job_events, job_logs
from inspire.cli.commands.ray import ray_commands
from inspire.cli.context import Context
from inspire.cli.main import main as cli_main


PICK_HELP = "Pick the Nth candidate (1-indexed) when the name is ambiguous."
notebook_lifecycle_module = importlib.import_module(
    "inspire.cli.commands.notebook.notebook_lifecycle"
)
notebook_metrics_module = importlib.import_module(
    "inspire.cli.commands.notebook.notebook_metrics"
)


@pytest.mark.parametrize(
    "path",
    (
        ("job", "status"),
        ("job", "instances"),
        ("job", "stop"),
        ("job", "delete"),
        ("job", "wait"),
        ("job", "command"),
        ("job", "shell"),
        ("job", "events"),
        ("job", "logs"),
        ("hpc", "status"),
        ("hpc", "instances"),
        ("hpc", "stop"),
        ("hpc", "delete"),
        ("hpc", "events"),
        ("ray", "status"),
        ("ray", "instances"),
        ("ray", "stop"),
        ("ray", "delete"),
        ("ray", "events"),
    ),
)
def test_name_resolving_commands_share_pick_help(path: tuple[str, str]) -> None:
    result = CliRunner().invoke(cli_main, [*path, "--help"])

    assert result.exit_code == 0, result.output
    assert "--pick INTEGER" in result.output
    assert PICK_HELP in " ".join(result.output.split())


@pytest.mark.parametrize("group", ("job", "hpc", "ray"))
@pytest.mark.parametrize("command", ("list", "create"))
def test_collection_and_create_commands_do_not_offer_pick(
    group: str,
    command: str,
) -> None:
    result = CliRunner().invoke(cli_main, [group, command, "--help"])

    assert result.exit_code == 0, result.output
    assert "--pick" not in result.output


@pytest.mark.parametrize(
    ("command", "result_value"),
    (
        ("status", {"name": "train-a", "status": "RUNNING"}),
        ("instances", ([], 0)),
        ("wait", ("job-live", {"name": "train-a", "status": "SUCCEEDED"})),
        ("command", {"command": "python train.py"}),
    ),
)
def test_job_readonly_commands_forward_pick(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    result_value: object,
) -> None:
    seen: list[tuple[int | None, bool]] = []
    monkeypatch.setattr(
        job_commands.Config,
        "from_files_and_env",
        lambda **_kwargs: (object(), {}),
    )
    monkeypatch.setattr(job_commands, "_close_web_client", lambda: None)

    def fake_readonly(**kwargs):  # noqa: ANN001, ANN202
        seen.append(
            (
                kwargs["pick"],
                kwargs["workspace_must_be_single"],
            )
        )
        return result_value

    monkeypatch.setattr(
        job_commands,
        "_run_readonly_web_job_operation",
        fake_readonly,
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "job",
            command,
            "train-a",
            "--workspace",
            "Training Room",
            "--pick",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen == [(2, True)]


def test_job_events_forwards_pick_to_retryable_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retryable: list[tuple[int | None, bool]] = []
    monkeypatch.setattr(
        job_events.Config,
        "from_files_and_env",
        lambda **_kwargs: (object(), {}),
    )
    monkeypatch.setattr(
        job_events,
        "_run_readonly_web_job_operation",
        lambda **kwargs: retryable.append(
            (kwargs["pick"], kwargs["workspace_must_be_single"])
        )
        or [],
    )
    monkeypatch.setattr(job_events, "_close_web_client", lambda: None)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "job",
            "events",
            "train-a",
            "--workspace",
            "Training Room",
            "--pick",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert retryable == [(2, True)]


def test_job_platform_logs_forwards_pick_to_retryable_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[int | None, bool]] = []
    monkeypatch.setattr(
        job_logs.Config,
        "from_files_and_env",
        lambda **_kwargs: (object(), {}),
    )
    monkeypatch.setattr(
        job_logs,
        "_run_readonly_web_job_operation",
        lambda **kwargs: (
            seen.append(
                (
                    kwargs["pick"],
                    kwargs["workspace_must_be_single"],
                )
            )
            or (
                "job-internal",
                object(),
                ["worker-0"],
                0,
                [],
                0,
            )
        ),
    )
    monkeypatch.setattr(job_logs, "_close_web_client", lambda: None)

    result = CliRunner().invoke(
        cli_main,
        [
            "job",
            "logs",
            "train-a",
            "--workspace",
            "Training Room",
            "--pick",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen == [(2, True)]


@pytest.mark.parametrize("follow", (False, True))
def test_job_ssh_logs_forwards_pick_to_selected_resolution_path(
    monkeypatch: pytest.MonkeyPatch,
    follow: bool,
) -> None:
    direct: list[tuple[int | None, bool]] = []
    retryable: list[tuple[int | None, bool]] = []
    monkeypatch.setattr(
        job_logs.Config,
        "from_files_and_env",
        lambda **_kwargs: (object(), {}),
    )
    monkeypatch.setattr(
        job_logs,
        "_resolve_web_job_id",
        lambda **kwargs: direct.append(
            (kwargs["pick"], kwargs["workspace_must_be_single"])
        )
        or "job-internal",
    )
    monkeypatch.setattr(
        job_logs,
        "_run_readonly_web_job_operation",
        lambda **kwargs: retryable.append(
            (kwargs["pick"], kwargs["workspace_must_be_single"])
        )
        or "job-internal",
    )
    monkeypatch.setattr(job_logs, "_close_web_client", lambda: None)
    monkeypatch.setattr(job_logs, "_run_job_logs_single_job", lambda *_args, **_kwargs: None)

    args = [
        "job",
        "logs",
        "train-a",
        "--workspace",
        "Training Room",
        "--source",
        "ssh",
        "--remote-log-path",
        "/logs/train.log",
        "--pick",
        "2",
    ]
    if follow:
        args.append("--follow")

    result = CliRunner().invoke(cli_main, args)

    assert result.exit_code == 0, result.output
    assert direct == ([] if follow else [(2, True)])
    assert retryable == ([(2, True)] if follow else [])


def test_job_stale_retry_preserves_pick_for_live_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions: list[tuple[int | None, bool, bool]] = []
    attempts: list[str] = []
    session = SimpleNamespace(all_workspace_names={"ws-1": "Training Room"})

    def fake_resolver(**kwargs):  # noqa: ANN001, ANN202
        resolutions.append(
            (
                kwargs["pick"],
                kwargs["workspace_must_be_single"],
                kwargs["require_live"],
            )
        )
        return "job-live" if kwargs["require_live"] else "job-stale"

    def operation(job_id: str, _session: object) -> str:
        attempts.append(job_id)
        if job_id == "job-stale":
            raise RuntimeError("job not found")
        return job_id

    monkeypatch.setattr(job_commands, "forget_resource_identity", lambda **_kwargs: None)

    result = job_commands._run_readonly_web_job_operation(
        job="train-a",
        workspace="Training Room",
        pick=2,
        workspace_must_be_single=True,
        session_factory=lambda: session,
        resolver=fake_resolver,
        operation=operation,
    )

    assert result == "job-live"
    assert resolutions == [(2, True, False), (2, True, True)]
    assert attempts == ["job-stale", "job-live"]


def test_hpc_instances_forwards_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int | None] = []
    monkeypatch.setattr(
        hpc_commands.Config,
        "from_files_and_env",
        lambda **_kwargs: (object(), {}),
    )
    monkeypatch.setattr(hpc_commands, "get_web_session", lambda: object())
    monkeypatch.setattr(
        hpc_commands,
        "_run_readonly_hpc_operation",
        lambda *_args, **kwargs: seen.append(kwargs["pick"]) or ([], 0),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "hpc",
            "instances",
            "prep-a",
            "--workspace",
            "CPU Room",
            "--pick",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen == [2]


def test_hpc_events_forwards_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int | None] = []
    monkeypatch.setattr(
        hpc_events.Config,
        "from_files_and_env",
        lambda **_kwargs: (object(), {}),
    )
    monkeypatch.setattr(hpc_events, "get_web_session", lambda: object())
    monkeypatch.setattr(
        hpc_events,
        "_run_readonly_hpc_operation",
        lambda *_args, **kwargs: seen.append(kwargs["pick"]) or [],
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "hpc",
            "events",
            "prep-a",
            "--workspace",
            "CPU Room",
            "--pick",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen == [2]


def test_ray_instances_forwards_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int | None] = []
    monkeypatch.setattr(
        ray_commands.Config,
        "from_files_and_env",
        lambda **_kwargs: (object(), {}),
    )
    monkeypatch.setattr(ray_commands, "get_web_session", lambda: object())
    monkeypatch.setattr(
        ray_commands,
        "_run_readonly_ray_operation",
        lambda *_args, **kwargs: seen.append(kwargs["pick"]) or ([], 0),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "ray",
            "instances",
            "pipeline-a",
            "--workspace",
            "CPU Room",
            "--pick",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen == [2]


def test_ray_events_forwards_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int | None] = []
    monkeypatch.setattr(
        ray_commands.Config,
        "from_files_and_env",
        lambda **_kwargs: (object(), {}),
    )
    monkeypatch.setattr(ray_commands, "get_web_session", lambda: object())
    monkeypatch.setattr(
        ray_commands,
        "_run_readonly_ray_operation",
        lambda *_args, **kwargs: seen.append(kwargs["pick"]) or [],
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "ray",
            "events",
            "pipeline-a",
            "--workspace",
            "CPU Room",
            "--pick",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen == [2]


def test_notebook_lifecycle_forwards_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int | None] = []
    monkeypatch.setattr(
        notebook_metrics_module,
        "_notebook_name_to_id",
        lambda _ctx, _name, pick=None: (
            seen.append(pick) or SimpleNamespace(task_id="notebook-internal")
        ),
    )
    monkeypatch.setattr(
        notebook_lifecycle_module,
        "list_notebook_runs",
        lambda _task_id: [],
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "notebook",
            "lifecycle",
            "demo-notebook",
            "--workspace",
            "Training Room",
            "--pick",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen == [2]


@pytest.mark.parametrize(
    ("module", "resolver_name", "runner_name"),
    (
        (
            hpc_commands,
            "_resolve_hpc_name_in_workspace",
            "_run_readonly_hpc_operation",
        ),
        (
            ray_commands,
            "_resolve_ray_name_in_workspace",
            "_run_readonly_ray_operation",
        ),
    ),
)
def test_hpc_and_ray_stale_retry_preserve_pick_for_live_resolution(
    monkeypatch: pytest.MonkeyPatch,
    module,
    resolver_name: str,
    runner_name: str,
) -> None:
    resolutions: list[tuple[int | None, bool]] = []
    attempts: list[str] = []

    def fake_resolver(*_args, **kwargs):  # noqa: ANN001, ANN202
        resolutions.append((kwargs["pick"], kwargs["require_live"]))
        return "resource-live" if kwargs["require_live"] else "resource-stale"

    def operation(resource_id: str, _session: object) -> str:
        attempts.append(resource_id)
        if resource_id == "resource-stale":
            raise RuntimeError("resource not found")
        return resource_id

    monkeypatch.setattr(module, resolver_name, fake_resolver)
    monkeypatch.setattr(module, "select_workspace_id", lambda *_args, **_kwargs: "ws-1")
    monkeypatch.setattr(module, "forget_resource_identity", lambda **_kwargs: None)

    result = getattr(module, runner_name)(
        Context(),
        session=object(),
        name="duplicate-name",
        workspace="CPU Room",
        limit=100,
        pick=2,
        operation=operation,
    )

    assert result == "resource-live"
    assert resolutions == [(2, False), (2, True)]
    assert attempts == ["resource-stale", "resource-live"]
