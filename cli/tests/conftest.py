"""Shared pytest fixtures for the CLI.

The CLI's resolvers reject platform handles at the user boundary: only
names cross normal command input. Some tests still exercise internal code
paths with pre-resolved handles; this autouse fixture short-circuits lookup
so those tests avoid live network calls while production code still enforces
the name-only contract.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_update_check(monkeypatch):  # noqa: ANN001
    """Tests should not spawn detached update-check subprocesses."""
    monkeypatch.setenv("INSPIRE_SKIP_UPDATE_CHECK", "1")


@pytest.fixture(autouse=True)
def _silence_normalize_environment(monkeypatch):  # noqa: ANN001
    """Stub `normalize_environment` to a no-op for the whole suite.

    `inspire account add` and `inspire notebook ssh` call
    `inspire.accounts.normalize_environment()` to check the local runtime.
    In tests that path would touch the real `~/.inspire/` directory of
    whoever runs pytest. Tests that need to exercise normalization itself
    (`test_account_normalize.py`) isolate `Path.home` and call
    `normalize_environment` directly, bypassing this stub.
    """
    from inspire.accounts import normalize as _normalize_module

    def _noop(**_kwargs):  # noqa: ANN003
        return _normalize_module.NormalizationReport()

    monkeypatch.setattr("inspire.accounts.normalize_environment", _noop)
    monkeypatch.setattr(_normalize_module, "normalize_environment", _noop)


@pytest.fixture(autouse=True)
def _no_orphan_state_sweep(monkeypatch):  # noqa: ANN001
    """Report no orphaned state unless a test opts in.

    `inspire update` sweeps `~/.inspire` for files no current version reads.
    Left unstubbed it would scan the real home of whoever runs pytest, so
    unrelated update tests would fail on that machine's leftovers.
    `test_state_inventory.py` isolates `Path.home` and undoes this stub.
    """
    from inspire.accounts import state_inventory

    monkeypatch.setattr(state_inventory, "find_orphan_state", lambda: [])


@pytest.fixture(autouse=True)
def _isolate_web_session_runtime(monkeypatch):  # noqa: ANN001
    """Keep web-session fallback state from leaking between tests."""
    from inspire.platform.web import session as web_session_module
    from inspire.platform.web.session.browser_client import _close_browser_client

    monkeypatch.setattr(web_session_module, "_BROWSER_API_FORCE_BROWSER", False)
    yield
    web_session_module._BROWSER_API_FORCE_BROWSER = False
    _close_browser_client()


@pytest.fixture(autouse=True)
def _isolate_notebook_target_resolver(monkeypatch):  # noqa: ANN001
    """Do not let tests scan the developer machine's real account caches."""
    import importlib

    target_resolver = importlib.import_module("inspire.cli.commands.notebook.target_resolver")

    monkeypatch.setattr(target_resolver, "current_account", lambda: None)
    monkeypatch.setattr(target_resolver, "list_accounts", lambda: [])
    monkeypatch.setattr(target_resolver, "account_exists", lambda _name: False)


@pytest.fixture(autouse=True)
def _stub_notebook_gpu_probe(monkeypatch, tmp_path):  # noqa: ANN001
    """Answer the notebook GPU probe locally: no GPU, therefore SSH-capable.

    The real probe opens a JupyterTerminal on the machine to read `nvidia-smi`.
    Every transport decision consumes it through `transport`, so stubbing it
    there keeps the suite off the network while `test_notebook_gpu_model.py`
    still exercises the probe and its cache directly. Tests that need a
    restricted machine set their own answer, which wins over this one.

    The probe's cache is redirected as well, so nothing reads or rewrites the
    real `~/.inspire/` file of whoever runs pytest.
    """
    import importlib

    gpu_model = importlib.import_module("inspire.cli.commands.notebook.gpu_model")
    transport = importlib.import_module("inspire.cli.commands.notebook.transport")

    monkeypatch.setattr(transport, "notebook_gpu_model", lambda **_kwargs: "")
    monkeypatch.setattr(gpu_model, "CACHE_FILE", tmp_path / "notebook-gpu-models.json")


@pytest.fixture(autouse=True)
def _short_circuit_platform_resolvers(monkeypatch):  # noqa: ANN001
    """Pass resolver arguments through untouched for internal-path tests.

    Production `resolve_job_id` etc. reject platform handles and force a
    name lookup. Real name-to-handle resolution is covered by unit tests of
    `resolve_by_name` / `resolve_job_id` that mock the list API directly.
    """

    def _passthrough(ctx, arg, **_kwargs):  # noqa: ANN001,ANN003
        return arg

    import importlib

    # Per-resource resolvers: module + attribute name.
    patches = [
        ("inspire.cli.commands.job.job_commands", "resolve_job_id"),
        ("inspire.cli.commands.job.job_events", "resolve_job_id"),
        ("inspire.cli.commands.job.job_logs", "resolve_job_id"),
        ("inspire.cli.commands.serving.serving_commands", "_resolve_serving_name"),
        ("inspire.cli.commands.image.image_commands", "_resolve_image_name"),
    ]

    # Notebook resolver returns (id, workspace_id) — wrap differently.
    def _nb_passthrough(ctx, *, identifier, **_kwargs):  # noqa: ANN001,ANN003
        return identifier, None

    try:
        import importlib as _il

        _nb_lookup = _il.import_module("inspire.cli.commands.notebook.notebook_lookup")
        monkeypatch.setattr(_nb_lookup, "_resolve_notebook_id", _nb_passthrough)
    except (ImportError, AttributeError):  # pragma: no cover
        pass
    for mod_name, attr in patches:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:  # pragma: no cover
            continue
        if hasattr(mod, attr):
            monkeypatch.setattr(mod, attr, _passthrough)
