from click.testing import CliRunner

from inspire.cli.commands.config.context import show_context
from inspire.cli.commands.config.context import _render_human


def test_config_context_renders_workspace_table(capsys):
    _render_human(
        {
            "active": {"account": "default", "project": None, "workspace": None},
            "projects": [{"name": "专项项目-2", "path": "special-project-2"}],
            "workspaces": ["CI-情境智能", "CPU资源空间"],
            "compute_groups": [],
            "accounts": [],
        }
    )

    output = capsys.readouterr().out
    assert "Active  account    default  project    -  workspace  -" in output
    assert "Projects" in output
    assert "Projects (1)" not in output
    assert "Workspaces" in output
    assert "Workspaces (2)" not in output
    assert "Name" in output
    assert "专项项目-2" in output
    assert "CI-情境智能" in output
    assert "CPU资源空间" in output
    assert "─" in output


def test_config_context_help_only_describes_name_inputs() -> None:
    result = CliRunner().invoke(show_context, ["--help"])

    assert result.exit_code == 0
    assert "Pass the displayed names" in result.output
    for internal_term in (" ID", "ws-", "project-", "lcg-", "handle"):
        assert internal_term not in result.output
