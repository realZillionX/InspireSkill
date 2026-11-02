import click
import pytest
from click.testing import CliRunner

from inspire.cli.main import main as cli_main


def _one_line(value: str) -> str:
    return " ".join(value.split())


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
