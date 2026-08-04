from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

from click.testing import CliRunner

from inspire.cli.commands.notebook import net_test as net_test_module
from inspire.cli.commands.notebook import notebook_lookup as notebook_lookup_module
from inspire.cli.context import Context
from inspire.cli.main import main as cli_main

workspace_module = importlib.import_module("inspire.config.workspaces")


def test_net_test_prints_human_status(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        net_test_module,
        "_resolve_notebook_for_net_test",
        lambda *_a, **_k: (
            SimpleNamespace(
                public_internet=False,
                public_successes=[],
                public_failures=["www.baidu.com:443"],
                endpoints=(),
            ),
            "gpu-box",
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        ["notebook", "net-test", "gpu-box", "--workspace", "分布式训练空间"],
    )

    assert result.exit_code == 0
    assert "Public internet: no" in result.output


def test_net_test_json(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        net_test_module,
        "_resolve_notebook_for_net_test",
        lambda *_a, **_k: (
            SimpleNamespace(
                public_internet=True,
                public_successes=["www.baidu.com:443"],
                public_failures=[],
                endpoints=(),
            ),
            "gpu-box",
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "notebook", "net-test", "gpu-box", "--workspace", "CPU资源空间"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)["data"]
    assert payload == {
        "notebook": "gpu-box",
        "public_internet": True,
        "public_successes": ["www.baidu.com:443"],
        "public_failures": [],
    }
    assert "nb-123" not in result.output


def test_net_test_probe_runs_through_stale_retry(monkeypatch) -> None:  # noqa: ANN001
    session = SimpleNamespace()
    seen: dict[str, object] = {}
    probe_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        net_test_module,
        "require_web_session",
        lambda *_args, **_kwargs: session,
    )
    monkeypatch.setattr(net_test_module, "load_config", lambda _ctx: SimpleNamespace())
    monkeypatch.setattr(net_test_module, "get_base_url", lambda: "https://example.invalid")
    monkeypatch.setattr(
        workspace_module,
        "resolve_workspace_query_scope",
        lambda *_args, **_kwargs: (["ws-live"], "ws-live"),
    )

    def fake_retry(*_args, operation, **kwargs):  # noqa: ANN001
        seen.update(kwargs)
        return operation("notebook-live"), "notebook-live", "ws-live"

    monkeypatch.setattr(
        notebook_lookup_module,
        "_run_notebook_operation_with_stale_handle_retry",
        fake_retry,
    )

    def fake_probe(**kwargs):  # noqa: ANN003
        probe_calls.append(kwargs)
        return SimpleNamespace(
            public_internet=False,
            public_successes=[],
            public_failures=[],
            endpoints=(),
        )

    monkeypatch.setattr(
        net_test_module.browser_api_module,
        "probe_notebook_network",
        fake_probe,
    )

    result, notebook_name = net_test_module._resolve_notebook_for_net_test(
        Context(),
        notebook="gpu-box",
        workspace="CPU资源空间",
        timeout=17,
    )

    assert result.public_internet is False
    assert notebook_name == "gpu-box"
    assert seen["identifier"] == "gpu-box"
    assert seen["workspace_ids"] == ["ws-live"]
    assert probe_calls == [
        {
            "notebook_id": "notebook-live",
            "session": session,
            "timeout": 17,
        }
    ]
