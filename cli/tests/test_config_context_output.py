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
    assert "Projects (1)" in output
    assert "Workspaces (2)" in output
    assert "Name" in output
    assert "专项项目-2" in output
    assert "CI-情境智能" in output
    assert "CPU资源空间" in output
    assert "─" in output
