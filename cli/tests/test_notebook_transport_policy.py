from __future__ import annotations

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from inspire.cli.commands.notebook import remote_exec as remote_exec_module
from inspire.cli.commands.notebook import transport as transport_module
from inspire.cli.context import EXIT_TIMEOUT
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
    compute_group: str = "训练区-H200-1号机房",
    status: str = "RUNNING",
) -> tuple[list[dict], list[dict], list[dict]]:
    """Stub name resolution, the GPU probe and detail; return their calls."""
    resolved: list[dict] = []
    probes: list[dict] = []
    details: list[dict] = []

    monkeypatch.setattr(transport_module, "require_web_session", lambda *_a, **_k: session)
    monkeypatch.setattr(transport_module, "get_base_url", lambda **_k: "https://example.test")

    def fake_resolve(*_args, **kwargs):  # noqa: ANN202
        resolved.append(kwargs)
        return "nb-123", "ws-123", compute_group

    def fake_probe(**kwargs):  # noqa: ANN202
        probes.append(kwargs)
        return gpu_model

    def fake_detail(**kwargs):  # noqa: ANN202
        details.append(kwargs)
        return {"status": status, "compute_group_name": compute_group}

    monkeypatch.setattr(transport_module, "_resolve_notebook_target", fake_resolve)
    monkeypatch.setattr(transport_module, "notebook_gpu_model", fake_probe)
    monkeypatch.setattr(
        transport_module.browser_api_module,
        "get_notebook_detail",
        fake_detail,
    )
    return resolved, probes, details


def test_preflight_blocks_ssh_when_the_machine_reports_h200(monkeypatch) -> None:  # noqa: ANN001
    session = SimpleNamespace(account="secondary")
    _resolved, probes, details = _patch_preflight(
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
    # The group came back with the name resolution, so no detail request.
    assert details == []
    assert probes == [
        {
            "notebook_id": "nb-123",
            "compute_group": "训练区-H200-1号机房",
            "session": session,
        }
    ]
    assert [call["require_live"] for call in _resolved] == [False, True]


def test_preflight_allows_ssh_when_the_machine_has_no_gpu(monkeypatch) -> None:  # noqa: ANN001
    session = SimpleNamespace(account="secondary")
    resolved, _probes, _details = _patch_preflight(
        monkeypatch,
        gpu_model="",
        session=session,
        compute_group="CPU资源-2",
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


def test_preflight_reads_the_group_from_detail_when_resolution_omits_it(monkeypatch) -> None:  # noqa: ANN001
    """The group is the probe's cache key, so it is worth one detail request."""
    session = SimpleNamespace(account="primary")
    _resolved, probes, details = _patch_preflight(
        monkeypatch,
        gpu_model="H100",
        session=session,
        compute_group="",
    )
    monkeypatch.setattr(
        transport_module.browser_api_module,
        "get_notebook_detail",
        lambda **kwargs: details.append(kwargs) or {"compute_group_name": "H100开发区"},
    )

    policy = transport_module.preflight_notebook_transport_policy(
        SimpleNamespace(json_output=False),
        notebook="gpu-box",
        workspace=None,
    )

    assert policy.allow_ssh is False
    assert details == [{"notebook_id": "nb-123", "session": session}]
    assert probes[0]["compute_group"] == "H100开发区"


def test_preflight_stops_when_the_notebook_is_not_running(monkeypatch, capsys) -> None:  # noqa: ANN001
    """A silent machine is almost always a stopped one; say so and stop."""
    session = SimpleNamespace(account="primary")
    _patch_preflight(monkeypatch, gpu_model=None, session=session, status="STOPPED")

    with pytest.raises(SystemExit) as exc:
        transport_module.preflight_notebook_transport_policy(
            SimpleNamespace(json_output=False),
            notebook="gpu-box",
            workspace=None,
        )

    assert exc.value.code != 0
    errors = capsys.readouterr().err
    assert "gpu-box is STOPPED" in errors
    assert "inspire notebook start gpu-box" in errors


def test_preflight_stops_when_a_running_notebook_stays_silent(monkeypatch, capsys) -> None:  # noqa: ANN001
    session = SimpleNamespace(account="primary")
    _patch_preflight(monkeypatch, gpu_model=None, session=session, status="RUNNING")

    with pytest.raises(SystemExit) as exc:
        transport_module.preflight_notebook_transport_policy(
            SimpleNamespace(json_output=False),
            notebook="gpu-box",
            workspace=None,
        )

    assert exc.value.code != 0
    errors = capsys.readouterr().err
    assert "JupyterTerminal did not respond" in errors
    assert "already re-resolved this notebook name from the live platform" in errors
    assert "manually refreshing caches should not be necessary" in errors


def test_preflight_replaces_stale_cached_notebook_before_jupyter(
    monkeypatch,
) -> None:  # noqa: ANN001
    session = SimpleNamespace(account="primary")
    resolved: list[bool] = []
    probes: list[str] = []

    monkeypatch.setattr(transport_module, "require_web_session", lambda *_a, **_k: session)
    monkeypatch.setattr(transport_module, "get_base_url", lambda **_k: "https://example.test")

    def fake_resolve(*_args, **kwargs):  # noqa: ANN202
        require_live = bool(kwargs["require_live"])
        resolved.append(require_live)
        if require_live:
            return "nb-current", "ws-123", "训练区-H100"
        return "nb-obsolete", "ws-123", "训练区-H100"

    def fake_probe(**kwargs):  # noqa: ANN202
        notebook_id = str(kwargs["notebook_id"])
        probes.append(notebook_id)
        return "H100" if notebook_id == "nb-current" else None

    monkeypatch.setattr(transport_module, "_resolve_notebook_target", fake_resolve)
    monkeypatch.setattr(transport_module, "notebook_gpu_model", fake_probe)

    policy = transport_module.preflight_notebook_transport_policy(
        SimpleNamespace(json_output=False),
        notebook="gpu-box",
        workspace=None,
    )

    assert policy.notebook_id == "nb-current"
    assert policy.exec_transport == "jupyter"
    assert resolved == [False, True]
    assert probes == ["nb-obsolete", "nb-current"]


def test_preflight_ignore_target_cache_starts_with_live_notebook_identity(
    monkeypatch,
) -> None:  # noqa: ANN001
    session = SimpleNamespace(account="primary")
    resolved, _probes, _details = _patch_preflight(
        monkeypatch,
        gpu_model="H100",
        session=session,
    )

    policy = transport_module.preflight_notebook_transport_policy(
        SimpleNamespace(json_output=False),
        notebook="gpu-box",
        workspace=None,
        ignore_target_cache=True,
    )

    assert policy.notebook_id == "nb-123"
    assert [call["require_live"] for call in resolved] == [True]


def test_jupyter_exec_missing_completion_marker_has_actionable_hint(
    monkeypatch,
    capsys,
) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        remote_exec_module.browser_api_module,
        "run_command_capture_in_notebook",
        lambda **_kwargs: SimpleNamespace(
            returncode=124,
            output="",
            completed=False,
        ),
    )

    code = remote_exec_module.try_exec_via_jupyter_terminal(
        SimpleNamespace(json_output=False),
        notebook_id="nb-current",
        command="hostname",
        session=SimpleNamespace(),
        remote_cwd=None,
        env_exports="",
        timeout_s=30,
    )

    assert code == EXIT_TIMEOUT
    errors = capsys.readouterr().err
    assert "did not establish or complete the remote command" in errors
    assert "inspire --debug notebook exec" in errors
    assert "manual cache refresh should not be needed" in errors


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
    local_file.write_text("x", encoding="utf-8")
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
