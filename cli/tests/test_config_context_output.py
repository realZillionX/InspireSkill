import json

import pytest
from click.testing import CliRunner

from inspire.cli.commands.config import context as context_module
from inspire.cli.commands.config.context import show_context
from inspire.cli.commands.config.context import _render_human
from inspire.cli.context import EXIT_VALIDATION_ERROR
from inspire.cli.main import main as cli_main
from inspire.config import Config


def test_config_context_renders_compact_name_lines(capsys):
    _render_human(
        {
            "active": {"account": "default", "project": None, "workspace": None},
            "projects": [{"name": "专项项目-2", "path": "internal-project-path"}],
            "workspaces": ["CI-情境智能", "CPU资源空间"],
            "compute_groups": [
                {
                    "name": "共享算力组",
                    "gpu_type": "internal-gpu-type",
                    "workspace": "CPU资源空间",
                }
            ],
            "accounts": [],
        }
    )

    output = capsys.readouterr().out
    assert "active account=default project=- workspace=-" in output
    assert "project 专项项目-2" in output
    assert "workspace CI-情境智能" in output
    assert "workspace CPU资源空间" in output
    assert "compute-group 共享算力组 workspace=CPU资源空间" in output
    assert "internal-project-path" not in output
    assert "internal-gpu-type" not in output


def test_config_context_help_only_describes_name_inputs() -> None:
    result = CliRunner().invoke(show_context, ["--help"])

    assert result.exit_code == 0
    assert "Pass the displayed names" in result.output
    for internal_term in (" ID", "ws-", "project-", "lcg-", "handle"):
        assert internal_term not in result.output


def _patch_large_context(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config(username="login-user", password="secret")
    cfg.context_project = "Project 00"
    cfg.context_workspace = "Workspace 00"
    cfg.projects = {
        f"Project {index:02d}": f"Project {index:02d}"
        for index in range(25)
    }
    cfg.project_catalog = {
        f"Project {index:02d}": {
            "name": f"Project {index:02d}",
            "path": f"/internal/project/{index}",
        }
        for index in range(25)
    }
    cfg.compute_groups = [
        {
            "name": f"Group {index:02d}",
            "gpu_type": f"GPU-{index}",
            "workspace_ids": [f"internal-workspace-{index}"],
        }
        for index in range(25)
    ]

    monkeypatch.setattr(
        context_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, **_: (cfg, {})),
    )
    monkeypatch.setattr("inspire.accounts.current_account", lambda: "primary")
    monkeypatch.setattr(
        "inspire.accounts.list_accounts",
        lambda: [f"Account {index:02d}" for index in range(25)],
    )
    monkeypatch.setattr(
        "inspire.config.workspaces.workspace_name_map",
        lambda _session: {
            f"internal-workspace-{index}": f"Workspace {index:02d}"
            for index in range(25)
        },
    )
    monkeypatch.setattr("inspire.platform.web.session.get_web_session", object)


def test_config_context_default_json_is_bounded_and_name_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_large_context(monkeypatch)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "--no-env-file", "config", "context"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    data = payload["data"]
    assert len(data["projects"]) == 20
    assert len(data["workspaces"]) == 20
    assert len(data["compute_groups"]) == 20
    assert len(data["accounts"]) == 20
    assert all(set(item) == {"name"} for item in data["projects"])
    assert all(set(item) <= {"name", "workspace"} for item in data["compute_groups"])
    assert data["truncated"] == {
        "projects": {"shown": 20, "total": 25},
        "workspaces": {"shown": 20, "total": 25},
        "compute_groups": {"shown": 20, "total": 25},
        "accounts": {"shown": 20, "total": 25},
    }
    assert "/internal/project/" not in result.output
    assert "GPU-" not in result.output


def test_config_context_limit_and_all_control_each_discovery_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_large_context(monkeypatch)
    runner = CliRunner()

    limited = runner.invoke(
        cli_main,
        ["--no-env-file", "config", "context", "--limit", "2"],
    )
    assert limited.exit_code == 0, limited.output
    assert "Project 00" in limited.output
    assert "Project 02" not in limited.output
    assert limited.output.count("Use --all for full lists.") == 1
    assert "projects 2/25" in limited.output

    unbounded = runner.invoke(
        cli_main,
        ["--json", "--no-env-file", "config", "context", "--all"],
    )
    assert unbounded.exit_code == 0, unbounded.output
    data = json.loads(unbounded.output)["data"]
    assert len(data["projects"]) == 25
    assert len(data["workspaces"]) == 25
    assert len(data["compute_groups"]) == 25
    assert len(data["accounts"]) == 25
    assert "truncated" not in data


def test_config_context_rejects_limit_with_all_as_single_json_document() -> None:
    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "--no-env-file",
            "config",
            "context",
            "--limit",
            "2",
            "--all",
        ],
    )

    assert result.exit_code == EXIT_VALIDATION_ERROR
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["error"]["type"] == "ValidationError"
    assert "either --limit or --all" in payload["error"]["message"]


def test_config_context_reports_actionable_workspace_discovery_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = Config(username="login-user", password="secret")
    monkeypatch.setattr(
        context_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, **_: (cfg, {})),
    )
    monkeypatch.setattr("inspire.accounts.current_account", lambda: "primary")
    monkeypatch.setattr("inspire.accounts.list_accounts", lambda: ["primary"])
    monkeypatch.setattr(
        "inspire.platform.web.session.get_web_session",
        lambda: (_ for _ in ()).throw(RuntimeError("/private/session.json")),
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "--no-env-file", "config", "context"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"] == {
        "active": {
            "account": "primary",
            "project": None,
            "workspace": None,
        },
        "projects": [],
        "workspaces": [],
        "compute_groups": [],
        "accounts": ["primary"],
        "warnings": [
            "Workspace names are unavailable. Run `inspire config check` and retry."
        ],
    }
