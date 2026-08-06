from __future__ import annotations

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from inspire.cli.commands.notebook import transport as transport_module
from inspire.cli.main import main as cli_main


@pytest.mark.parametrize(
    ("compute_group", "expected"),
    [
        ("训练区-H200-1号机房", False),
        ("开发区-H100-cuda12.8版本-119核", False),
        ("h200-2号机房", False),
        ("NVIDIA_H100_SXM", False),
        ("CPU资源-2", True),
        ("4090 开发区", True),
        ("", True),
    ],
)
def test_group_supports_ssh(compute_group: str, expected: bool) -> None:
    assert transport_module.group_supports_ssh(compute_group) is expected


def _patch_preflight(
    monkeypatch,  # noqa: ANN001
    *,
    resolved_group: str,
    session: object,
    detail: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """Stub name resolution and detail lookup; return their recorded calls."""
    resolved: list[dict] = []
    detail_calls: list[dict] = []

    monkeypatch.setattr(transport_module, "require_web_session", lambda *_a, **_k: session)
    monkeypatch.setattr(transport_module, "get_base_url", lambda **_k: "https://example.test")

    def fake_resolve(*_args, **kwargs):  # noqa: ANN202
        resolved.append(kwargs)
        return "nb-123", "ws-123", resolved_group

    def fake_detail(**kwargs):  # noqa: ANN202
        detail_calls.append(kwargs)
        return detail or {}

    monkeypatch.setattr(transport_module, "_resolve_notebook_target", fake_resolve)
    monkeypatch.setattr(
        transport_module.browser_api_module,
        "get_notebook_detail",
        fake_detail,
    )
    return resolved, detail_calls


def test_preflight_blocks_ssh_for_h200_group(monkeypatch) -> None:  # noqa: ANN001
    session = SimpleNamespace(account="secondary")
    _resolved, detail_calls = _patch_preflight(
        monkeypatch,
        resolved_group="训练区-H200-1号机房",
        session=session,
    )

    policy = transport_module.preflight_notebook_transport_policy(
        SimpleNamespace(json_output=False),
        notebook="gpu-box",
        workspace=None,
        account="secondary",
    )

    assert policy.compute_group == "训练区-H200-1号机房"
    assert policy.allow_ssh is False
    assert policy.allow_proxy_url is False
    assert policy.exec_transport == "jupyter"
    assert policy.session is session
    # The group came back with the name resolution, so no detail request.
    assert detail_calls == []


def test_preflight_allows_ssh_for_cpu_group(monkeypatch) -> None:  # noqa: ANN001
    session = SimpleNamespace(account="secondary")
    resolved, detail_calls = _patch_preflight(
        monkeypatch,
        resolved_group="CPU资源-2",
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
    assert detail_calls == []


def test_preflight_falls_back_to_detail_when_group_missing(monkeypatch) -> None:  # noqa: ANN001
    session = SimpleNamespace(account="primary")
    _resolved, detail_calls = _patch_preflight(
        monkeypatch,
        resolved_group="",
        session=session,
        detail={"compute_group_name": "H100开发区"},
    )

    policy = transport_module.preflight_notebook_transport_policy(
        SimpleNamespace(json_output=False),
        notebook="gpu-box",
        workspace=None,
    )

    assert policy.compute_group == "H100开发区"
    assert policy.allow_ssh is False
    assert detail_calls == [{"notebook_id": "nb-123", "session": session}]


def test_policy_blocks_ssh_for_restricted_group() -> None:
    policy = transport_module.NotebookTransportPolicy(
        notebook="gpu-box",
        notebook_id="nb-123",
        compute_group="训练区-H200-1号机房",
    )

    assert policy.allow_ssh is False
    assert policy.exec_transport == "jupyter"
    assert "JupyterTerminal" in policy.block_hint


def test_policy_allows_ssh_for_unrestricted_group() -> None:
    policy = transport_module.NotebookTransportPolicy(
        notebook="cpu-box",
        notebook_id="nb-456",
        compute_group="CPU资源-2",
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
            compute_group="训练区-H200-1号机房",
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
    assert "训练区-H200-1号机房" in result.output


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
            compute_group="训练区-H200-1号机房",
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
