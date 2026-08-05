from __future__ import annotations

import json

from click.testing import CliRunner

from inspire.bridge.tunnel import BridgeProfile, TunnelConfig
from inspire.cli.commands import workload_profile
from inspire.cli.commands.notebook import connection as connection_module
from inspire.cli.commands.notebook import path_aliases as path_aliases_module
from inspire.cli.commands.serving import serving_commands as serving_module
from inspire.cli.main import main as cli_main
from inspire.config import Config


def _assert_default_page(data: dict, key: str) -> None:
    assert len(data[key]) == 20
    assert data["shown"] == 20
    assert data["total"] == 25
    assert data["truncated"] is True


def test_profile_list_defaults_to_twenty(monkeypatch) -> None:  # noqa: ANN001
    profiles = {
        f"profile-{index:02d}": {
            "workspace": "GPU Workspace",
            "project": "Research",
            "group": "H200 Group",
            "quota": "1,20,200",
            "image": "train:v1",
        }
        for index in range(25)
    }
    config = Config(username="", password="", profiles={"job": profiles})
    monkeypatch.setattr(
        workload_profile.Config,
        "from_files_and_env",
        classmethod(lambda cls, **_kwargs: (config, {})),
    )

    result = CliRunner().invoke(cli_main, ["--json", "job", "profile", "list"])

    assert result.exit_code == 0, result.output
    _assert_default_page(json.loads(result.output)["data"], "items")


def test_path_alias_list_supports_explicit_all(monkeypatch) -> None:  # noqa: ANN001
    aliases = {
        f"alias-{index:02d}": f"/inspire/ssd/project/demo/{index:02d}/"
        for index in range(25)
    }
    monkeypatch.setattr(
        path_aliases_module,
        "load_project_path_aliases",
        lambda: (None, aliases),
    )

    default_result = CliRunner().invoke(
        cli_main,
        ["--json", "notebook", "path", "list"],
    )
    all_result = CliRunner().invoke(
        cli_main,
        ["--json", "notebook", "path", "list", "--all"],
    )

    assert default_result.exit_code == 0, default_result.output
    default_data = json.loads(default_result.output)["data"]
    _assert_default_page(default_data, "items")
    assert all(set(item) == {"name"} for item in default_data["items"])
    assert "/inspire/" not in default_result.output
    assert all_result.exit_code == 0, all_result.output
    all_data = json.loads(all_result.output)["data"]
    assert len(all_data["items"]) == 25
    assert all(set(item) == {"name"} for item in all_data["items"])
    assert "/inspire/" not in all_result.output
    assert "truncated" not in all_data


def test_connection_list_verifies_only_visible_page(monkeypatch) -> None:  # noqa: ANN001
    tunnel_config = TunnelConfig()
    for index in range(25):
        tunnel_config.add_bridge(
            BridgeProfile(
                name=f"notebook-{index:02d}",
                notebook_name=f"notebook-{index:02d}",
                proxy_url=f"https://proxy.invalid/{index}/",
                workspace_name="CPU Workspace",
            )
        )
    verified: list[str] = []
    monkeypatch.setattr(connection_module, "load_tunnel_config", lambda: tunnel_config)
    monkeypatch.setattr(
        connection_module,
        "is_tunnel_available",
        lambda **kwargs: verified.append(kwargs["bridge_name"]) or True,
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "notebook", "connection", "list", "--verify"],
    )

    assert result.exit_code == 0, result.output
    _assert_default_page(json.loads(result.output)["data"], "items")
    assert len(verified) == 20


def test_connection_target_list_defaults_to_twenty(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        connection_module,
        "list_notebook_targets",
        lambda: [
            {
                "name": f"notebook-{index:02d}",
                "account": "alice",
                "workspace": "CPU Workspace",
            }
            for index in range(25)
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "notebook", "connection", "target", "list"],
    )

    assert result.exit_code == 0, result.output
    _assert_default_page(json.loads(result.output)["data"], "items")


def test_serving_configs_defaults_to_twenty(monkeypatch) -> None:  # noqa: ANN001
    config = Config(username="", password="")

    class _ServingSession:
        all_workspace_ids = ["workspace"]
        all_workspace_names = {"workspace": "GPU Workspace"}

    monkeypatch.setattr(
        serving_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, **_kwargs: (config, {})),
    )
    monkeypatch.setattr(serving_module, "get_web_session", _ServingSession)
    monkeypatch.setattr(
        serving_module.browser_api_module,
        "get_serving_configs",
        lambda **_kwargs: {
            "configs": {
                "enable_auto_stop": True,
                "items": [
                    {
                        "name": f"choice-{index:02d}",
                        "gpu_count_min": 1,
                        "gpu_count_max": 8,
                    }
                    for index in range(25)
                ],
            }
        },
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "serving", "configs", "--workspace", "GPU Workspace"],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    _assert_default_page(data, "items")
    assert "auto_stop" not in data
    assert all(item["auto_stop"] is True for item in data["items"])
