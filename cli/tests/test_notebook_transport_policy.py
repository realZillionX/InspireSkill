from __future__ import annotations

from types import SimpleNamespace

from click.testing import CliRunner

from inspire.cli.commands.notebook import transport as transport_module
from inspire.cli.main import main as cli_main


def test_preflight_skips_live_probe_for_statically_restricted_gpu(monkeypatch) -> None:  # noqa: ANN001
    session = SimpleNamespace(account="secondary")
    probe_calls: list[dict] = []

    monkeypatch.setattr(transport_module, "require_web_session", lambda *_a, **_k: session)
    monkeypatch.setattr(
        transport_module,
        "_resolve_notebook_id",
        lambda *_a, **_k: ("nb-123", "ws-123"),
    )
    monkeypatch.setattr(transport_module, "get_base_url", lambda **_k: "https://example.test")
    monkeypatch.setattr(
        transport_module.browser_api_module,
        "get_notebook_detail",
        lambda **_k: {
            "resource_spec_price": None,
            "node": {"gpu_info": {"gpu_product_simple": "H200-SXM"}},
        },
    )
    monkeypatch.setattr(
        transport_module.browser_api_module,
        "probe_notebook_network",
        lambda **kwargs: probe_calls.append(kwargs),
    )

    policy = transport_module.preflight_notebook_transport_policy(
        SimpleNamespace(json_output=False),
        notebook="gpu-box",
        workspace=None,
        account="secondary",
    )

    assert policy.exec_transport == "jupyter"
    assert policy.reason == "static_gpu_policy"
    assert policy.session is session
    assert probe_calls == []


def test_preflight_still_live_probes_potentially_public_gpu(monkeypatch) -> None:  # noqa: ANN001
    session = SimpleNamespace(account="secondary")
    probe = SimpleNamespace(public_internet=True)
    probe_calls: list[dict] = []
    resolved: dict[str, object] = {}

    monkeypatch.setattr(transport_module, "require_web_session", lambda *_a, **_k: session)

    def fake_resolve(*_args, **kwargs):  # noqa: ANN202
        resolved.update(kwargs)
        return "nb-456", "ws-123"

    monkeypatch.setattr(transport_module, "_resolve_notebook_id", fake_resolve)
    monkeypatch.setattr(transport_module, "get_base_url", lambda **_k: "https://example.test")
    monkeypatch.setattr(
        transport_module.browser_api_module,
        "get_notebook_detail",
        lambda **_k: {
            "resource_spec_price": {"gpu_info": {"gpu_product_simple": "RTX 4090"}}
        },
    )

    def fake_probe(**kwargs):  # noqa: ANN202
        probe_calls.append(kwargs)
        return probe

    monkeypatch.setattr(
        transport_module.browser_api_module,
        "probe_notebook_network",
        fake_probe,
    )

    policy = transport_module.preflight_notebook_transport_policy(
        SimpleNamespace(json_output=False),
        notebook="cpu-box",
        workspace=None,
        account="secondary",
        timeout=17,
        pick=2,
    )

    assert policy.exec_transport == "ssh"
    assert policy.reason == "live_probe"
    assert resolved["pick"] == 2
    assert probe_calls == [
        {"notebook_id": "nb-456", "session": session, "timeout": 17}
    ]


def test_policy_blocks_ssh_when_public_internet_false() -> None:
    policy = transport_module.NotebookTransportPolicy(
        notebook="gpu-box",
        notebook_id="nb-123",
        public_internet=False,
        reason="live_probe",
    )

    assert policy.allow_ssh is False
    assert policy.exec_transport == "jupyter"
    assert "JupyterTerminal" in policy.block_hint


def test_policy_allows_ssh_when_public_internet_true() -> None:
    policy = transport_module.NotebookTransportPolicy(
        notebook="cpu-box",
        notebook_id="nb-456",
        public_internet=True,
        reason="live_probe",
    )

    assert policy.allow_ssh is True
    assert policy.exec_transport == "ssh"


def test_ssh_command_blocks_restricted_notebook_before_bootstrap(monkeypatch) -> None:  # noqa: ANN001
    from inspire.cli.commands.notebook import ssh as ssh_module

    monkeypatch.setattr(
        ssh_module,
        "preflight_notebook_transport_policy",
        lambda *_a, **_k: transport_module.NotebookTransportPolicy(
            notebook="gpu-box",
            notebook_id="nb-123",
            public_internet=False,
            reason="live_probe",
        ),
    )
    called = {"run": False}
    monkeypatch.setattr(
        ssh_module,
        "run_notebook_ssh",
        lambda **_k: called.__setitem__("run", True),
    )

    result = CliRunner().invoke(
        cli_main,
        ["notebook", "ssh", "gpu-box", "--workspace", "分布式训练空间"],
    )

    assert result.exit_code != 0
    assert called["run"] is False
    assert "blocked on notebooks without public internet" in result.output


def test_notebook_scp_rejects_restricted_notebook_with_cp_hint(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    from inspire.cli.commands.notebook import remote_scp as scp_module

    local_file = tmp_path / "config.yaml"
    local_file.write_text("x")
    monkeypatch.setattr(
        scp_module,
        "preflight_notebook_transport_policy",
        lambda *_a, **_k: transport_module.NotebookTransportPolicy(
            notebook="gpu-box",
            notebook_id="nb-123",
            public_internet=False,
            reason="live_probe",
        ),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "notebook",
            "scp",
            "gpu-box",
            str(local_file),
            "/inspire/hdd/project/topic/user/config.yaml",
        ],
    )

    assert result.exit_code != 0
    assert "SSH-based" in result.output
    assert "public-internet notebook" in result.output
    assert "/inspire/" in result.output
