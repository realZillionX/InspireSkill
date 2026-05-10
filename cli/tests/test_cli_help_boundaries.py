from click.testing import CliRunner

from inspire.cli.main import main as cli_main


def _one_line(value: str) -> str:
    return " ".join(value.split())


def test_job_logs_help_positions_web_as_fallback() -> None:
    result = CliRunner().invoke(cli_main, ["job", "logs", "--help"])
    output = _one_line(result.output)

    assert result.exit_code == 0
    assert "CLI-managed remote log file" in output
    assert "cached notebook SSH bridge" in output
    assert "Fallback: read platform aggregated logs" in output


def test_instances_help_uses_required_workspace_and_num() -> None:
    for group in ("job", "ray", "hpc"):
        result = CliRunner().invoke(cli_main, [group, "instances", "--help"])
        output = _one_line(result.output)

        assert result.exit_code == 0
        assert "--workspace TEXT" in result.output
        assert "Required; -A is not accepted" in output
        assert "--num INTEGER" in result.output
        assert "--web" not in result.output
        assert "--all-workspaces" not in result.output
        assert "--all-users" not in result.output
        assert "--page-num" not in result.output
        assert "--page-size" not in result.output
        assert "--max-pages" not in result.output


def test_resources_nodes_help_prefers_min_nodes_wording() -> None:
    result = CliRunner().invoke(cli_main, ["resources", "nodes", "--help"])

    assert result.exit_code == 0
    assert "whole 8-GPU nodes" in result.output
    assert "inspire resources nodes --min-nodes 2" in result.output
    assert "not scattered GPUs" in result.output


def test_dry_run_help_says_resolve_not_submit() -> None:
    for args in (
        ["job", "create", "--help"],
        ["hpc", "create", "--help"],
    ):
        result = CliRunner().invoke(cli_main, args)
        output = _one_line(result.output)

        assert result.exit_code == 0
        assert "without calling the create API" in output
        assert "Resolve workspace, project, quota" in output


def test_job_create_help_explains_framework_and_fault_tolerance() -> None:
    result = CliRunner().invoke(cli_main, ["job", "create", "--help"])
    output = _one_line(result.output)

    assert result.exit_code == 0
    assert "Platform training-framework label sent to the create API" in output
    assert "does not choose the Docker image" in output
    assert "auto-restart the training job after failures" in output
    assert "Max platform restart attempts" in output
    assert "Ignored when fault tolerance is off" in output


def test_init_help_explains_plain_init_discovery() -> None:
    result = CliRunner().invoke(cli_main, ["init", "--help"])
    output = _one_line(result.output)
    removed_flag = "--" + "discover"

    assert result.exit_code == 0
    assert "Plain `inspire init` logs in or uses the active account" in output
    assert removed_flag not in output
    assert "asks which storage tier the `me` path alias should use" in output
    assert "top-level `me` points at the selected path tier" in output
    assert "`ssd` suggested for the path hot tier" in output
    assert "Legacy: detect env vars" in output


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


def test_job_batch_help_keeps_scope_small() -> None:
    result = CliRunner().invoke(cli_main, ["job", "batch", "--help"])
    output = _one_line(result.output)

    assert result.exit_code == 0
    assert "Submit a JSON/TOML matrix through `job create`" in result.output
    assert "top-level `jobs` is required" in output
    assert "condition fields may come from `profile = \"<name>\"`" in output
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
    assert "condition fields may come from `profile = \"<name>\"`" in output


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


def test_status_catalog_help_marks_diagnostic_scope() -> None:
    result = CliRunner().invoke(cli_main, ["job", "status-catalog", "--help"])
    output = _one_line(result.output)

    assert result.exit_code == 0
    assert "Maintainer diagnostic" in output
    assert "Use `job status <name>` for normal job inspection" in output


def test_events_help_has_no_cache_mode() -> None:
    for args in (
        ["job", "events", "--help"],
        ["notebook", "events", "--help"],
        ["hpc", "events", "--help"],
    ):
        result = CliRunner().invoke(cli_main, args)

        assert result.exit_code == 0
        assert "--from-cache" not in result.output


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
