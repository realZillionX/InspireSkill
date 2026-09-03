import click
import pytest
from click.testing import CliRunner

from inspire.cli.main import main as cli_main


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _help_option_order(value: str) -> list[str]:
    options = value.split("Options:", 1)[1]
    ordered: list[str] = []
    for line in options.splitlines():
        if not line.startswith("  -"):
            continue
        stripped = line.strip()
        declaration = stripped.split("  ", 1)[0]
        for token in declaration.replace(",", " ").replace("/", " ").split():
            if token.startswith("--"):
                ordered.append(token)
                break
    return ordered


def _public_command_paths() -> list[list[str]]:
    paths: list[list[str]] = []

    def visit(command: click.Command, path: list[str]) -> None:
        paths.append(path)
        if isinstance(command, click.Group):
            for name, child in sorted(command.commands.items()):
                if not child.hidden:
                    visit(child, [*path, name])

    visit(cli_main, [])
    return paths


def test_all_public_help_is_name_only() -> None:
    forbidden = (
        " login id",
        "raw id",
        "platform id",
        "platform handle",
        "partial handle",
        "uuid",
    )
    runner = CliRunner()

    for path in _public_command_paths():
        result = runner.invoke(cli_main, [*path, "--help"] if path else ["--help"])
        output = _one_line(result.output).lower()

        assert result.exit_code == 0, " ".join(path)
        for term in forbidden:
            assert term not in output, f"{' '.join(path) or '<root>'}: {term}"


def test_job_logs_help_positions_platform_as_default() -> None:
    result = CliRunner().invoke(cli_main, ["job", "logs", "--help"])
    output = _one_line(result.output)

    assert result.exit_code == 0
    assert "explicitly named remote log file" in output
    assert "cached notebook bridge" in output
    assert "Platform logs are the default" in output


def test_job_create_help_states_full_node_multi_node_constraint() -> None:
    result = CliRunner().invoke(cli_main, ["job", "create", "--help"])
    output = _one_line(result.output)

    assert result.exit_code == 0
    assert "Multi-node jobs only support full 8-GPU nodes." in output
    assert "the job uses N x 8 GPUs total" in output
    assert "N x 2, N x 4, and N x 6 layouts are not supported" in output
    assert "Values greater than 1 require a full 8-GPU quota on every node" in output


def test_instances_help_uses_required_workspace_and_limit() -> None:
    for group in ("job", "ray", "hpc"):
        result = CliRunner().invoke(cli_main, [group, "instances", "--help"])

        assert result.exit_code == 0
        assert "--workspace NAME" in result.output
        assert "--limit INTEGER" in result.output
        assert "--all" in result.output


@pytest.mark.parametrize(
    "path",
    (
        ["job", "status"],
        ["hpc", "status"],
        ["ray", "status"],
        ["serving", "status"],
        ["model", "status"],
        ["project", "detail"],
        ["image", "detail"],
    ),
)
def test_named_detail_commands_share_name_and_pick_interface(path: list[str]) -> None:
    result = CliRunner().invoke(cli_main, [*path, "--help"])

    assert result.exit_code == 0
    output = _one_line(result.output)
    assert " [OPTIONS] NAME" in output
    assert "--pick INTEGER" in result.output
    assert "Pick the Nth candidate (1-indexed) when the name is ambiguous." in output


def test_resources_nodes_help_prefers_min_nodes_wording() -> None:
    result = CliRunner().invoke(cli_main, ["resources", "nodes", "--help"])
    output = _one_line(result.output)

    assert result.exit_code == 0
    assert "whole 8-GPU nodes" in result.output
    assert "compute group name keyword/substring" in output
    assert "full name is not required" in output
    assert "inspire resources nodes --workspace 分布式训练空间 --min-nodes 2" in result.output
    assert "not scattered GPUs" in result.output


def test_resources_group_help_separates_workspace_and_node_event_scope() -> None:
    result = CliRunner().invoke(cli_main, ["resources", "--help"])
    output = _one_line(result.output)

    assert result.exit_code == 0, result.output
    assert "Node events are cluster facts" in output
    assert "therefore take no workspace option" in output
    assert "  nodes " not in result.output


def test_query_commands_require_explicit_workspace() -> None:
    cases = (
        ["job", "list"],
        ["notebook", "status", "demo"],
        ["notebook", "list"],
        ["resources", "availability"],
        ["resources", "nodes"],
        ["hpc", "list"],
        ["ray", "list"],
        ["model", "list"],
        ["serving", "list"],
        ["serving", "configs"],
        ["account", "permissions"],
        ["image", "list"],
        ["image", "detail", "demo"],
    )
    runner = CliRunner()
    for args in cases:
        result = runner.invoke(cli_main, args)
        assert result.exit_code != 0
        assert "Missing option '--workspace'" in result.output


@pytest.mark.parametrize(
    "path",
    (
        ["job", "list"],
        ["hpc", "list"],
        ["ray", "list"],
        ["notebook", "list"],
        ["serving", "list"],
        ["model", "list"],
        ["account", "permissions"],
    ),
)
def test_workspace_collection_commands_share_query_contract(path: list[str]) -> None:
    result = CliRunner().invoke(cli_main, [*path, "--help"])
    output = _one_line(result.output)

    assert result.exit_code == 0, result.output
    assert "--workspace NAME|all" in result.output
    assert "Workspace name or 'all'." in output
    assert "-n, --limit INTEGER RANGE" in output
    assert "--all" in result.output


@pytest.mark.parametrize(
    "path",
    (
        ["job", "list"],
        ["hpc", "list"],
        ["ray", "list"],
        ["notebook", "list"],
        ["serving", "list"],
        ["model", "list"],
        ["account", "permissions"],
    ),
)
def test_workspace_collection_commands_reject_limit_with_all(
    path: list[str],
) -> None:
    result = CliRunner().invoke(
        cli_main,
        [*path, "--workspace", "all", "--limit", "1", "--all"],
    )

    assert result.exit_code != 0
    assert "Use either --limit or --all, not both." in result.output


def test_query_group_help_says_keyword_substring() -> None:
    for args in (
        ["job", "quota", "--help"],
        ["resources", "availability", "--help"],
        ["resources", "nodes", "--help"],
    ):
        result = CliRunner().invoke(cli_main, args)
        output = _one_line(result.output)

        assert result.exit_code == 0
        assert "compute group name keyword/substring" in output
        assert "full name is not required" in output


def test_create_help_requires_full_group_name() -> None:
    for args in (
        ["notebook", "create", "--help"],
        ["job", "create", "--help"],
        ["hpc", "create", "--help"],
        ["ray", "create", "--help"],
        ["serving", "create", "--help"],
    ):
        result = CliRunner().invoke(cli_main, args)
        output = _one_line(result.output)

        assert result.exit_code == 0
        assert "Full compute group name" in output
        assert "same quota row as --quota" in output


def test_create_help_explains_workspace_aware_priority() -> None:
    for group in ("notebook", "job", "hpc", "ray", "serving"):
        result = CliRunner().invoke(cli_main, [group, "create", "--help"])
        output = _one_line(result.output)

        assert result.exit_code == 0
        assert "Fair-scheduling workspaces accept 1=LOW" in output
        assert "4=HIGH (default: 4)" in output
        assert "other workspaces accept 1-10 (default: 10)" in output
        assert "selected project's platform policy may cap" in output


def test_dry_run_help_says_resolve_not_submit() -> None:
    for args in (
        ["job", "create", "--help"],
        ["hpc", "create", "--help"],
    ):
        result = CliRunner().invoke(cli_main, args)
        output = _one_line(result.output)

        assert result.exit_code == 0
        assert "without submitting" in output
        assert "Resolve workspace, project, quota" in output


def test_job_create_help_explains_framework_and_fault_tolerance() -> None:
    result = CliRunner().invoke(cli_main, ["job", "create", "--help"])
    output = _one_line(result.output)

    assert result.exit_code == 0
    assert "Training framework label shown by the platform" in output
    assert "does not choose the Docker image" in output
    assert "auto-restart the training job after failures" in output
    assert "Max platform restart attempts" in output
    assert "Ignored when fault tolerance is off" in output
    assert "--shm-size INTEGER" in result.output
    assert "Overrides INSPIRE_SHM_SIZE/job.shm_size" in output
    assert "platform shm_gi" in output
    assert "--enable-notification / --no-enable-notification" in output
    assert "Feishu status notifications" in output
    assert "INSPIRE_JOB_ENABLE_NOTIFICATION" in output
    assert "[job].enable_notification" in output


def test_notebook_create_help_explains_auto_stop_boundary() -> None:
    result = CliRunner().invoke(cli_main, ["notebook", "create", "--help"])
    output = _one_line(result.output)

    assert result.exit_code == 0
    assert "Request idle auto-stop" in output
    assert "does not disable manager auto-recycle rules" in output
    assert "workspace lifetime caps" in output


def test_init_help_is_account_only() -> None:
    result = CliRunner().invoke(cli_main, ["init", "--help"])
    output = _one_line(result.output)

    assert result.exit_code == 0
    assert "writes only ``~/.inspire/accounts/<account>/config.toml``" in output
    assert "no repository-local config is read or created" in output
    assert "--scope" not in result.output
    assert "--no-discover" in result.output


def test_root_help_explains_global_options() -> None:
    result = CliRunner().invoke(cli_main, ["--help"])
    output = _one_line(result.output)

    assert result.exit_code == 0
    assert "--json prints structured script output" in output


def test_parser_errors_scrub_id_shaped_values_before_root_callback() -> None:
    for value in (
        "job-123456",
        "123e4567-e89b-12d3-a456-426614174000",
        "deadbeef",
    ):
        result = CliRunner().invoke(
            cli_main,
            [
                "job",
                "logs",
                "name",
                "--workspace",
                "CPU临时测试空间",
                "--source",
                value,
            ],
        )

        assert result.exit_code == 2
        assert value not in result.output
        assert "<redacted>" in result.output


def test_parser_errors_scrub_path_values_before_root_callback() -> None:
    value = "/Users/alice/private/missing.pub"
    result = CliRunner().invoke(
        cli_main,
        [
            "notebook",
            "ssh-config",
            "demo",
            "--workspace",
            "CPU临时测试空间",
            "--pubkey",
            value,
        ],
    )

    assert result.exit_code == 2
    assert value not in result.output
    assert "<redacted>" in result.output


def test_notebook_help_exposes_current_command_surface() -> None:
    help_result = CliRunner().invoke(cli_main, ["notebook", "--help"])

    assert help_result.exit_code == 0
    for command in ("list", "status", "create", "ssh", "connection", "metrics", "events"):
        assert f"\n  {command} " in help_result.output


def test_notebook_ssh_and_connection_help_expose_current_interfaces() -> None:
    notebook_help = CliRunner().invoke(cli_main, ["notebook", "--help"])
    ssh_help = CliRunner().invoke(cli_main, ["notebook", "ssh", "--help"])
    connection_help = CliRunner().invoke(cli_main, ["notebook", "connection", "--help"])

    assert notebook_help.exit_code == 0
    for command in ("connection", "ssh-config", "ssh-proxy"):
        assert f"\n  {command} " in notebook_help.output

    assert ssh_help.exit_code == 0
    assert "OpenSSH access for SSH-capable notebooks" in ssh_help.output
    assert "NAME" in ssh_help.output
    assert "--workspace NAME" in ssh_help.output

    assert connection_help.exit_code == 0
    for subcommand in ("list", "status", "refresh", "forget", "prune", "target"):
        assert f"\n  {subcommand} " in connection_help.output


@pytest.mark.parametrize(
    "path",
    (
        ["notebook", "ssh"],
        ["notebook", "ssh-config"],
        ["notebook", "ssh-proxy"],
        ["notebook", "exec"],
        ["notebook", "shell"],
        ["notebook", "scp"],
        ["notebook", "install-deps"],
    ),
)
def test_cached_notebook_transport_commands_share_target_selector_contract(
    path: list[str],
) -> None:
    result = CliRunner().invoke(cli_main, [*path, "--help"])

    assert result.exit_code == 0, result.output
    output = _one_line(result.output)
    assert " NAME" in output
    assert "--workspace NAME" in result.output
    assert "--account NAME" in result.output
    assert "--pick INTEGER" in result.output
    assert "Workspace name used to disambiguate this notebook target." in output


@pytest.mark.parametrize(
    "args",
    (
        ["notebook", "ssh", "demo", "--workspace", "all"],
        ["notebook", "ssh-config", "demo", "--workspace", "all"],
        ["notebook", "ssh-proxy", "demo", "--workspace", "all"],
        ["notebook", "exec", "demo", "--workspace", "all", "true"],
        ["notebook", "shell", "demo", "--workspace", "all"],
        ["notebook", "scp", "demo", "--workspace", "all", "src", "dst"],
        ["notebook", "install-deps", "demo", "--workspace", "all", "--slurm"],
    ),
)
def test_cached_notebook_transport_commands_reject_workspace_all(
    args: list[str],
) -> None:
    result = CliRunner().invoke(cli_main, args)

    assert result.exit_code == 2
    assert "--workspace requires one workspace name for this command." in result.output


@pytest.mark.parametrize(
    "args",
    (
        ["notebook", "ssh", "demo"],
        ["notebook", "ssh-config", "demo"],
        ["notebook", "ssh-proxy", "demo"],
        ["notebook", "exec", "demo", "true"],
        ["notebook", "shell", "demo"],
        ["notebook", "scp", "demo", "src", "dst"],
        ["notebook", "install-deps", "demo", "--slurm"],
    ),
)
def test_cached_notebook_transport_commands_reject_raw_workspace_id(
    args: list[str],
) -> None:
    raw_workspace_id = "ws-12345678-1234-1234-1234-123456789abc"
    result = CliRunner().invoke(
        cli_main,
        [*args[:3], "--workspace", raw_workspace_id, *args[3:]],
    )

    assert result.exit_code == 2
    assert "Workspace selection is invalid. Pass a visible workspace name." in result.output
    assert raw_workspace_id not in result.output


def test_job_batch_help_keeps_scope_small() -> None:
    result = CliRunner().invoke(cli_main, ["job", "batch", "--help"])
    output = _one_line(result.output)

    assert result.exit_code == 0
    assert "Submit a JSON/TOML matrix through `job create`" in result.output
    assert "top-level `jobs` is required" in output
    assert "scheduling conditions explicitly" in output
    assert "Required fields after expansion:" in result.output
    assert "Optional fields use create-command defaults" in result.output


def test_hpc_batch_help_keeps_scope_small() -> None:
    result = CliRunner().invoke(cli_main, ["hpc", "batch", "--help"])

    assert result.exit_code == 0
    assert "Submit a JSON/TOML matrix through `hpc create`" in result.output
    assert "Required fields after expansion:" in result.output
    assert "name, entrypoint, quota, workspace, project, group, image" in result.output


@pytest.mark.parametrize("kind", ("notebook", "job", "hpc", "ray", "serving"))
def test_workload_batch_config_argument_uses_path_metavar(kind: str) -> None:
    result = CliRunner().invoke(cli_main, [kind, "batch", "--help"])

    assert result.exit_code == 0, result.output
    usage = result.output.splitlines()[0]
    assert usage.endswith("[OPTIONS] PATH")
    assert "CONFIG_PATH" not in usage


def test_notebook_batch_help_keeps_scope_small() -> None:
    result = CliRunner().invoke(cli_main, ["notebook", "batch", "--help"])
    output = _one_line(result.output)

    assert result.exit_code == 0
    assert "Create notebook instances from a JSON/TOML matrix" in result.output
    assert "Top-level `notebooks` is required" in result.output
    assert "scheduling conditions listed below" in output


def test_ray_and_serving_batch_help_keeps_scope_small() -> None:
    ray_result = CliRunner().invoke(cli_main, ["ray", "batch", "--help"])
    serving_result = CliRunner().invoke(cli_main, ["serving", "batch", "--help"])
    serving_output = _one_line(serving_result.output)

    assert ray_result.exit_code == 0
    assert "Create Ray jobs from a JSON/TOML matrix" in ray_result.output
    assert "Head and worker scheduling conditions" in ray_result.output
    assert serving_result.exit_code == 0
    assert "Create inference servings from a JSON/TOML matrix" in serving_result.output
    assert "including every scheduling condition" in serving_output


def test_events_help_uses_live_tail_options() -> None:
    for args in (
        ["job", "events", "--help"],
        ["notebook", "events", "--help"],
        ["hpc", "events", "--help"],
        ["ray", "events", "--help"],
        ["serving", "events", "--help"],
    ):
        result = CliRunner().invoke(cli_main, args)
        output = _one_line(result.output)

        assert result.exit_code == 0
        assert "--follow" in result.output
        assert "--tail INTEGER" in result.output
        assert "Maximum recent events to display." in output
        assert "[default: 20; x>=1]" in output

    notebook_result = CliRunner().invoke(cli_main, ["notebook", "events", "--help"])
    assert notebook_result.exit_code == 0
    assert "--keyword" in notebook_result.output


@pytest.mark.parametrize(
    "path",
    (
        ["job", "list"],
        ["hpc", "list"],
        ["ray", "list"],
        ["notebook", "list"],
        ["serving", "list"],
    ),
)
def test_workload_list_status_filters_share_metavar(path: list[str]) -> None:
    result = CliRunner().invoke(cli_main, [*path, "--help"])

    assert result.exit_code == 0, result.output
    assert "-s, --status STATUS" in _one_line(result.output)


@pytest.mark.parametrize(
    "path",
    (
        ["job", "list"],
        ["hpc", "list"],
        ["ray", "list"],
        ["notebook", "list"],
        ["serving", "list"],
    ),
)
def test_workload_list_keyword_filters_share_metavar(path: list[str]) -> None:
    result = CliRunner().invoke(cli_main, [*path, "--help"])

    assert result.exit_code == 0, result.output
    assert "--keyword KEYWORD" in result.output


def test_hpc_and_ray_list_filters_follow_shared_option_order() -> None:
    for path in (["hpc", "list"], ["ray", "list"]):
        result = CliRunner().invoke(cli_main, [*path, "--help"])

        assert result.exit_code == 0, result.output
        output = _one_line(result.output)
        positions = [
            output.index("--workspace NAME|all"),
            output.index("-s, --status STATUS"),
            output.index("--keyword KEYWORD"),
            output.index("-n, --limit INTEGER RANGE"),
            output.index("--all"),
        ]
        assert positions == sorted(positions)


@pytest.mark.parametrize(
    ("path", "ordered_options"),
    (
        (
            ["notebook", "events"],
            ("--workspace", "--pick", "--keyword", "--tail", "--follow", "--interval"),
        ),
        (
            ["job", "events"],
            (
                "--workspace",
                "--pick",
                "--type",
                "--reason",
                "--tail",
                "--follow",
                "--interval",
            ),
        ),
        (
            ["hpc", "events"],
            ("--workspace", "--pick", "--reason", "--tail", "--follow", "--interval"),
        ),
        (
            ["ray", "events"],
            ("--workspace", "--pick", "--type", "--reason", "--tail", "--follow", "--interval"),
        ),
        (
            ["serving", "events"],
            ("--workspace", "--pick", "--type", "--reason", "--tail", "--follow", "--interval"),
        ),
    ),
)
def test_events_help_orders_common_options_consistently(
    path: list[str],
    ordered_options: tuple[str, ...],
) -> None:
    result = CliRunner().invoke(cli_main, [*path, "--help"])

    assert result.exit_code == 0, result.output
    option_order = _help_option_order(result.output)
    positions = [option_order.index(option) for option in ordered_options]
    assert positions == sorted(positions)


@pytest.mark.parametrize(
    "path",
    (
        ["notebook", "start"],
        ["notebook", "stop"],
        ["notebook", "delete"],
        ["notebook", "status"],
        ["notebook", "events"],
        ["notebook", "lifecycle"],
        ["notebook", "metrics"],
        ["notebook", "save-image"],
        ["notebook", "cancel-save-image"],
        ["job", "status"],
        ["job", "stop"],
        ["job", "delete"],
        ["job", "events"],
        ["job", "metrics"],
        ["hpc", "status"],
        ["hpc", "stop"],
        ["hpc", "delete"],
        ["hpc", "events"],
        ["hpc", "metrics"],
        ["serving", "status"],
        ["serving", "stop"],
        ["serving", "delete"],
        ["serving", "metrics"],
        ["image", "detail"],
        ["image", "set-visibility"],
        ["image", "delete"],
    ),
)
def test_resource_arguments_use_name_metavar(path: list[str]) -> None:
    result = CliRunner().invoke(cli_main, [*path, "--help"])

    assert result.exit_code == 0, result.output
    assert " NAME" in _one_line(result.output.split("Options:", 1)[0])


@pytest.mark.parametrize(
    "path",
    (
        ["notebook", "lifecycle"],
        ["notebook", "save-image"],
        ["notebook", "cancel-save-image"],
        ["image", "set-visibility"],
    ),
)
def test_name_resolving_commands_expose_shared_pick_option(path: list[str]) -> None:
    result = CliRunner().invoke(cli_main, [*path, "--help"])

    assert result.exit_code == 0, result.output
    assert "--pick INTEGER" in result.output
    assert (
        "Pick the Nth candidate (1-indexed) when the name is ambiguous."
        in _one_line(result.output)
    )


def test_destructive_commands_share_yes_help() -> None:
    for path in (
        ["account", "remove"],
        ["cache", "clear"],
        ["notebook", "delete"],
        ["notebook", "connection", "forget"],
        ["notebook", "connection", "prune"],
        ["notebook", "connection", "target", "forget"],
        ["job", "delete"],
        ["hpc", "delete"],
        ["ray", "delete"],
        ["serving", "delete"],
        ["image", "delete"],
        ["tensorboard", "delete"],
    ):
        result = CliRunner().invoke(cli_main, [*path, "--help"])

        assert result.exit_code == 0, result.output
        assert "-y, --yes" in result.output
        assert "Skip the interactive confirmation prompt." in _one_line(result.output)


@pytest.mark.parametrize(
    ("path", "expected"),
    (
        (
            ["job", "create"],
            (
                "--name NAME",
                "--workspace NAME",
                "--project NAME",
                "--group NAME",
                "--quota SPEC",
                "--image NAME|URL",
            ),
        ),
        (
            ["hpc", "create"],
            (
                "--name NAME",
                "--workspace NAME",
                "--project NAME",
                "--group NAME",
                "--quota SPEC",
                "--image NAME|URL",
            ),
        ),
        (
            ["notebook", "create"],
            (
                "--name NAME",
                "--workspace NAME",
                "--project NAME",
                "--group NAME",
                "--quota SPEC",
                "--image NAME|URL",
            ),
        ),
        (
            ["ray", "create"],
            (
                "--name NAME",
                "--workspace NAME",
                "--project NAME",
                "--group NAME",
                "--quota SPEC",
                "--image NAME|URL",
                "--worker SPEC",
            ),
        ),
        (
            ["serving", "create"],
            (
                "--name NAME",
                "--model NAME",
                "--workspace NAME",
                "--project NAME",
                "--group NAME",
                "--quota SPEC",
                "--image NAME|URL",
            ),
        ),
    ),
)
def test_workload_create_help_uses_name_and_spec_metavars(
    path: list[str],
    expected: tuple[str, ...],
) -> None:
    result = CliRunner().invoke(cli_main, [*path, "--help"])

    assert result.exit_code == 0, result.output
    output = _one_line(result.output)
    for option in expected:
        assert option in output


@pytest.mark.parametrize(
    ("path", "ordered_options"),
    (
        (
            ["notebook", "create"],
            (
                "--name",
                "--workspace",
                "--project",
                "--group",
                "--quota",
                "--image",
                "--shm-size",
            ),
        ),
        (
            ["job", "create"],
            (
                "--name",
                "--command",
                "--workspace",
                "--project",
                "--group",
                "--quota",
                "--image",
                "--framework",
            ),
        ),
        (
            ["hpc", "create"],
            (
                "--name",
                "--entrypoint",
                "--workspace",
                "--project",
                "--group",
                "--quota",
                "--image",
                "--image-type",
            ),
        ),
        (
            ["ray", "create"],
            (
                "--name",
                "--command",
                "--workspace",
                "--project",
                "--group",
                "--quota",
                "--image",
                "--description",
            ),
        ),
        (
            ["serving", "create"],
            (
                "--name",
                "--model",
                "--model-version",
                "--command",
                "--port",
                "--workspace",
                "--project",
                "--group",
                "--quota",
                "--image",
                "--replicas",
            ),
        ),
    ),
)
def test_workload_create_help_orders_common_scheduling_selectors(
    path: list[str],
    ordered_options: tuple[str, ...],
) -> None:
    result = CliRunner().invoke(cli_main, [*path, "--help"])

    assert result.exit_code == 0, result.output
    option_order = _help_option_order(result.output)
    positions = [option_order.index(option) for option in ordered_options]
    assert positions == sorted(positions)


def test_image_and_model_help_expose_current_visibility_and_source_options() -> None:
    runner = CliRunner()
    save_result = runner.invoke(cli_main, ["notebook", "save-image", "--help"])
    visibility_result = runner.invoke(
        cli_main,
        ["image", "set-visibility", "--help"],
    )
    register_result = runner.invoke(cli_main, ["model", "register", "--help"])

    assert save_result.exit_code == 0
    assert "--workspace NAME" in save_result.output
    # Three visibilities, matching the web picker's 个人可见 / 项目可见 / 公开可见.
    assert "--visibility [private|project|public]" in save_result.output
    assert visibility_result.exit_code == 0
    assert "--visibility [private|project|public]" in visibility_result.output
    assert register_result.exit_code == 0
    assert "--source-path PATH" in register_result.output


def test_notebook_scp_help_is_ssh_only() -> None:
    result = CliRunner().invoke(cli_main, ["notebook", "scp", "--help"])

    assert result.exit_code == 0
    assert "SCP" in result.output
    assert "SSH-capable notebook" in result.output
    assert "/inspire/" in result.output


def test_notebook_proxy_url_help_warns_the_url_is_a_credential() -> None:
    """The one command that emits a platform URL must say what that URL is.

    It embeds a short-lived token, so it reaches the notebook for anyone who
    holds it — and it lands in agent transcripts and shell history by design.
    """
    result = CliRunner().invoke(cli_main, ["notebook", "proxy-url", "--help"])

    assert result.exit_code == 0
    assert "token" in result.output.lower()
    assert "credential" in result.output.lower()
    assert "handle" not in result.output.lower()
    assert " id " not in f" {result.output.lower()} "


def test_notebook_ssh_config_help_mentions_rsync_conversion() -> None:
    result = CliRunner().invoke(cli_main, ["notebook", "ssh-config", "--help"])

    assert result.exit_code == 0
    assert "rsync" in result.output
    assert "SSH-capable notebook" in result.output
    assert "/inspire/" in result.output
