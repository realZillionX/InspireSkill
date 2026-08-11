from __future__ import annotations

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from inspire.cli.commands.notebook import transport as transport_module
from inspire.cli.main import main as cli_main


@pytest.mark.parametrize(
    ("gpu_model", "expected"),
    [
        ("H200", False),
        ("H100", False),
        ("NVIDIA H100 80GB HBM3", False),
        ("h200", False),
        ("4090", True),
        ("A100", True),
        # No GPU on the machine, and a machine that never answered.
        ("", True),
        (None, True),
    ],
)
def test_gpu_model_supports_ssh(gpu_model: str | None, expected: bool) -> None:
    assert transport_module.gpu_model_supports_ssh(gpu_model) is expected


def _patch_preflight(
    monkeypatch,  # noqa: ANN001
    *,
    gpu_model: str | None,
    session: object,
) -> tuple[list[dict], list[dict]]:
    """Stub name resolution and the GPU probe; return their recorded calls."""
    resolved: list[dict] = []
    probes: list[dict] = []

    monkeypatch.setattr(transport_module, "require_web_session", lambda *_a, **_k: session)
    monkeypatch.setattr(transport_module, "get_base_url", lambda **_k: "https://example.test")

    def fake_resolve(*_args, **kwargs):  # noqa: ANN202
        resolved.append(kwargs)
        return "nb-123", "ws-123"

    def fake_probe(**kwargs):  # noqa: ANN202
        probes.append(kwargs)
        return gpu_model

    monkeypatch.setattr(transport_module, "_resolve_notebook_id", fake_resolve)
    monkeypatch.setattr(transport_module, "notebook_gpu_model", fake_probe)
    return resolved, probes


def test_preflight_blocks_ssh_when_the_machine_reports_h200(monkeypatch) -> None:  # noqa: ANN001
    session = SimpleNamespace(account="secondary")
    _resolved, probes = _patch_preflight(
        monkeypatch,
        gpu_model="H200",
        session=session,
    )

    policy = transport_module.preflight_notebook_transport_policy(
        SimpleNamespace(json_output=False),
        notebook="gpu-box",
        workspace=None,
        account="secondary",
    )

    assert policy.gpu_model == "H200"
    assert policy.allow_ssh is False
    assert policy.allow_proxy_url is False
    assert policy.exec_transport == "jupyter"
    assert policy.session is session
    assert probes == [{"notebook_id": "nb-123", "session": session}]


def test_preflight_allows_ssh_when_the_machine_has_no_gpu(monkeypatch) -> None:  # noqa: ANN001
    session = SimpleNamespace(account="secondary")
    resolved, _probes = _patch_preflight(
        monkeypatch,
        gpu_model="",
        session=session,
    )

    policy = transport_module.preflight_notebook_transport_policy(
        SimpleNamespace(json_output=False),
        notebook="cpu-box",
        workspace=None,
        account="secondary",
        pick=2,
    )

    assert policy.allow_ssh is True
    assert policy.exec_transport == "ssh"
    assert resolved[0]["pick"] == 2


def test_preflight_allows_ssh_when_the_machine_does_not_answer(monkeypatch) -> None:  # noqa: ANN001
    """An unread model leaves SSH as the only transport that can still work."""
    session = SimpleNamespace(account="primary")
    _patch_preflight(monkeypatch, gpu_model=None, session=session)

    policy = transport_module.preflight_notebook_transport_policy(
        SimpleNamespace(json_output=False),
        notebook="gpu-box",
        workspace=None,
    )

    assert policy.gpu_model is None
    assert policy.allow_ssh is True
    assert policy.exec_transport == "ssh"


def test_policy_blocks_ssh_for_restricted_gpu() -> None:
    policy = transport_module.NotebookTransportPolicy(
        notebook="gpu-box",
        notebook_id="nb-123",
        gpu_model="H200",
    )

    assert policy.allow_ssh is False
    assert policy.exec_transport == "jupyter"
    assert "JupyterTerminal" in policy.block_hint


def test_policy_allows_ssh_for_unrestricted_gpu() -> None:
    policy = transport_module.NotebookTransportPolicy(
        notebook="dev-box",
        notebook_id="nb-456",
        gpu_model="4090",
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
            gpu_model="H200",
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
    assert "blocked on H100/H200 notebooks" in result.output
    assert "runs on H200 GPUs" in result.output


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
            gpu_model="H200",
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
    assert "SSH-capable notebook" in result.output
    assert "/inspire/" in result.output
