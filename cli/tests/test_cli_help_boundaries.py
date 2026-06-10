from click.testing import CliRunner

from inspire.cli.main import main as cli_main


def _one_line(value: str) -> str:
    return " ".join(value.split())


def test_job_logs_help_positions_web_as_fallback() -> None:
    result = CliRunner().invoke(cli_main, ["job", "logs", "--help"])
    output = _one_line(result.output)

    assert result.exit_code == 0
    assert "CLI-managed remote log file" in output
    assert "cached notebook bridge" in output
    assert "Platform logs are the default" in output


def test_instances_help_uses_required_workspace_and_limit() -> None:
    for group in ("job", "ray", "hpc"):
        result = CliRunner().invoke(cli_main, [group, "instances", "--help"])

        assert result.exit_code == 0
        assert "--workspace TEXT" in result.output
        assert "--limit INTEGER" in result.output
        assert "--num" not in result.output
        assert "--web" not in result.output
        assert "--all-workspaces" not in result.output
        assert "--all-users" not in result.output
        assert "--page-num" not in result.output
        assert "--page-size" not in result.output
        assert "--max-pages" not in result.output


def test_resources_nodes_help_prefers_min_nodes_wording() -> None:
    result = CliRunner().invoke(cli_main, ["resources", "nodes", "--help"])
    output = _one_line(result.output)

    assert result.exit_code == 0
    assert "whole 8-GPU nodes" in result.output
    assert "compute group name keyword/substring" in output
    assert "full name is not required" in output
    assert "inspire resources nodes --workspace 分布式训练空间 --min-nodes 2" in result.output
    assert "not scattered GPUs" in result.output


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
        ["user", "permissions"],
    )
    runner = CliRunner()
    for args in cases:
        result = runner.invoke(cli_main, args)
        assert result.exit_code != 0
        assert "Missing option '--workspace'" in result.output


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


def test_create_and_profile_group_help_requires_full_name() -> None:
    for args in (
        ["notebook", "create", "--help"],
        ["job", "create", "--help"],
        ["hpc", "create", "--help"],
        ["ray", "create", "--help"],
        ["serving", "create", "--help"],
        ["notebook", "profile", "set", "--help"],
        ["job", "profile", "set", "--help"],
        ["hpc", "profile", "set", "--help"],
        ["ray", "profile", "set", "--help"],
        ["serving", "profile", "set", "--help"],
    ):
        result = CliRunner().invoke(cli_main, args)
        output = _one_line(result.output)

        assert result.exit_code == 0
        assert "Full compute group name" in output
        assert "Partial matches accepted" not in output
        assert "compute group name keyword/substring" not in output


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


def test_init_help_explains_plain_init_discovery() -> None:
    result = CliRunner().invoke(cli_main, ["init", "--help"])
    output = _one_line(result.output)
    removed_flag = "--" + "discover"

    assert result.exit_code == 0
    assert "Plain `inspire init` defaults to global scope" in output
    assert removed_flag not in output
    assert "writes account-level catalogs and remote path aliases" in output
    assert "writes this repository's project context and path-alias overrides" in output
    assert "top-level `me` points at the selected path tier" in output
    assert "`ssd` suggested for the path hot tier" in output
    assert "--scope [project|global]" in result.output
    assert "--no-discover" in result.output
    assert "--global" not in result.output
    assert "--project, -p" not in result.output
    assert "--json" not in result.output


def test_init_rejects_removed_discover_flag() -> None:
    removed_flag = "--" + "discover"
    result = CliRunner().invoke(cli_main, ["init", removed_flag])

    assert result.exit_code != 0
    assert f"No such option: {removed_flag}" in result.output


def test_root_help_explains_global_options() -> None:
    result = CliRunner().invoke(cli_main, ["--help"])
    output = _one_line(result.output)

    assert result.exit_code == 0
    assert "--json prints structured script output" in output
    assert "--profile" not in output
    assert "INSPIRE_PROFILE" not in output


def test_root_profile_option_is_removed() -> None:
    result = CliRunner().invoke(cli_main, ["--profile", "h200", "--help"])

    assert result.exit_code != 0
    assert "No such option: --profile" in result.output


def test_top_level_batch_command_is_removed() -> None:
    result = CliRunner().invoke(cli_main, ["batch", "--help"])

    assert result.exit_code != 0
    assert "No such command 'batch'" in result.output


def test_notebook_top_command_is_removed() -> None:
    help_result = CliRunner().invoke(cli_main, ["notebook", "--help"])
    result = CliRunner().invoke(cli_main, ["notebook", "top"])

    assert help_result.exit_code == 0
    assert "\n  top " not in help_result.output
    assert result.exit_code != 0
    assert "No such command 'top'" in result.output


def test_notebook_ssh_removed_compat_commands_and_connection_group() -> None:
    notebook_help = CliRunner().invoke(cli_main, ["notebook", "--help"])
    ssh_help = CliRunner().invoke(cli_main, ["notebook", "ssh", "--help"])
    connection_help = CliRunner().invoke(cli_main, ["notebook", "connection", "--help"])

    assert notebook_help.exit_code == 0
    for removed in ("connections", "refresh", "forget", "test"):
        assert f"\n  {removed} " not in notebook_help.output
        result = CliRunner().invoke(cli_main, ["notebook", removed, "--help"])
        assert result.exit_code != 0
        assert f"No such command '{removed}'" in result.output
    for command in ("connection", "ssh-config", "ssh-proxy"):
        assert f"\n  {command} " in notebook_help.output

    assert ssh_help.exit_code == 0
    assert "Open SSH to a notebook or run a remote command" in ssh_help.output
    for subcommand in ("connect", "refresh", "forget", "test"):
        assert f"\n  {subcommand} " not in ssh_help.output
    for removed in ("list", "status", "exec", "shell", "scp", "install-deps"):
        assert f"\n  {removed} " not in ssh_help.output

    assert connection_help.exit_code == 0
    for subcommand in ("list", "status", "refresh", "forget", "prune", "target"):
        assert f"\n  {subcommand} " in connection_help.output


def test_job_batch_help_keeps_scope_small() -> None:
    result = CliRunner().invoke(cli_main, ["job", "batch", "--help"])
    output = _one_line(result.output)

    assert result.exit_code == 0
    assert "Submit a JSON/TOML matrix through `job create`" in result.output
    assert "top-level `jobs` is required" in output
    assert 'condition fields may come from `profile = "<name>"`' in output
    assert "Required fields after expansion:" in result.output
    assert "Optional fields use create-command defaults" in result.output


def test_hpc_batch_help_keeps_scope_small() -> None:
    result = CliRunner().invoke(cli_main, ["hpc", "batch", "--help"])

    assert result.exit_code == 0
    assert "Submit a JSON/TOML matrix through `hpc create`" in result.output
    assert "Required fields after expansion:" in result.output
    assert "name, entrypoint, quota, workspace, project, group, image" in result.output


def test_notebook_batch_help_keeps_scope_small() -> None:
    result = CliRunner().invoke(cli_main, ["notebook", "batch", "--help"])
    output = _one_line(result.output)

    assert result.exit_code == 0
    assert "Create notebook instances from a JSON/TOML matrix" in result.output
    assert "Top-level `notebooks` is required" in result.output
    assert 'condition fields may come from `profile = "<name>"`' in output


def test_ray_and_serving_batch_help_keeps_scope_small() -> None:
    ray_result = CliRunner().invoke(cli_main, ["ray", "batch", "--help"])
    serving_result = CliRunner().invoke(cli_main, ["serving", "batch", "--help"])
    serving_output = _one_line(serving_result.output)

    assert ray_result.exit_code == 0
    assert "Create Ray jobs from a JSON/TOML matrix" in ray_result.output
    assert "Worker objects may also set" in ray_result.output
    assert serving_result.exit_code == 0
    assert "Create inference servings from a JSON/TOML matrix" in serving_result.output
    assert "Condition fields may" in serving_output


def test_events_help_has_no_cache_mode() -> None:
    for args in (
        ["job", "events", "--help"],
        ["notebook", "events", "--help"],
        ["hpc", "events", "--help"],
        ["ray", "events", "--help"],
    ):
        result = CliRunner().invoke(cli_main, args)

        assert result.exit_code == 0
        assert "--from-cache" not in result.output
        assert "--follow" in result.output
        assert "--watch" not in result.output
        assert "Alias for global --json" not in result.output
        assert "Equivalent to top-level" not in result.output

    notebook_result = CliRunner().invoke(cli_main, ["notebook", "events", "--help"])
    assert notebook_result.exit_code == 0
    assert "--keyword" in notebook_result.output
    assert "--type" not in notebook_result.output
    assert "--reason" not in notebook_result.output


def test_model_help_has_no_cross_user_filter() -> None:
    for subcommand in ("list", "status", "versions"):
        result = CliRunner().invoke(cli_main, ["model", subcommand, "--help"])

        assert result.exit_code == 0
        assert "--mine" not in result.output


def test_serving_help_has_no_all_users_mode() -> None:
    for subcommand in ("list", "status", "stop", "delete"):
        result = CliRunner().invoke(cli_main, ["serving", subcommand, "--help"])

        assert result.exit_code == 0
        assert "--all" not in result.output


def test_ray_create_help_has_no_raw_json_body_escape_hatch() -> None:
    result = CliRunner().invoke(cli_main, ["ray", "create", "--help"])

    assert result.exit_code == 0
    assert "--json-body" not in result.output
    assert "--head-image" not in result.output
    assert "--head-group" not in result.output
    assert "--head-quota" not in result.output
    assert "--head-shm" not in result.output
    assert "--image TEXT" in result.output
    assert "--group TEXT" in result.output
    assert "--quota TEXT" in result.output


def test_operation_help_has_no_command_local_json_or_removed_confirm_flags() -> None:
    runner = CliRunner()
    cases = (
        ["init", "--help"],
        ["config", "show", "--help"],
        ["config", "check", "--help"],
        ["config", "context", "--help"],
        ["project", "list", "--help"],
        ["notebook", "lifecycle", "--help"],
        ["notebook", "metrics", "--help"],
        ["job", "metrics", "--help"],
        ["hpc", "metrics", "--help"],
        ["serving", "metrics", "--help"],
        ["image", "register", "--help"],
        ["image", "save", "--help"],
        ["image", "set-visibility", "--help"],
        ["image", "delete", "--help"],
    )
    for args in cases:
        result = runner.invoke(cli_main, args)
        assert result.exit_code == 0, args
        assert "Alias for global --json" not in result.output
        assert "Equivalent to top-level" not in result.output

    delete_result = runner.invoke(cli_main, ["image", "delete", "--help"])
    assert "--force" not in delete_result.output
    assert "--yes" in delete_result.output

    ssh_delete = runner.invoke(cli_main, ["user", "ssh-keys", "delete", "--help"])
    assert ssh_delete.exit_code == 0
    assert "--force" not in ssh_delete.output
    assert "--yes" in ssh_delete.output


def test_removed_visibility_and_model_source_options_are_absent() -> None:
    runner = CliRunner()
    for args in (
        ["image", "save", "--help"],
        ["image", "set-visibility", "--help"],
        ["model", "register", "--help"],
    ):
        result = runner.invoke(cli_main, args)
        assert result.exit_code == 0, args
        assert "--public" not in result.output
        assert "--private" not in result.output
        assert "--source-type" not in result.output

    save_result = runner.invoke(cli_main, ["image", "save", "--help"])
    assert "--workspace TEXT" in save_result.output
    assert "--visibility [private|public]" in save_result.output


def test_resources_availability_has_no_watch_option() -> None:
    result = CliRunner().invoke(cli_main, ["resources", "availability", "--help"])

    assert result.exit_code == 0
    assert "--watch" not in result.output
    assert "--interval" not in result.output
