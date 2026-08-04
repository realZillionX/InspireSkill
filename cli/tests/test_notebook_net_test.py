from __future__ import annotations

import json
from types import SimpleNamespace

from click.testing import CliRunner

from inspire.cli.commands.notebook import net_test as net_test_module
from inspire.cli.main import main as cli_main


def test_net_test_prints_human_status(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        net_test_module,
        "_resolve_notebook_for_net_test",
        lambda *_a, **_k: (object(), "nb-123", "gpu-box"),
    )
    monkeypatch.setattr(
        net_test_module.browser_api_module,
        "probe_notebook_network",
        lambda **_k: SimpleNamespace(
            public_internet=False,
            public_successes=[],
            public_failures=["www.baidu.com:443"],
            endpoints=(),
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
        lambda *_a, **_k: (object(), "nb-123", "gpu-box"),
    )
    monkeypatch.setattr(
        net_test_module.browser_api_module,
        "probe_notebook_network",
        lambda **_k: SimpleNamespace(
            public_internet=True,
            public_successes=["www.baidu.com:443"],
            public_failures=[],
            endpoints=(),
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
