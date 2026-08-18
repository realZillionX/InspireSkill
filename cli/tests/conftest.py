"""Shared pytest fixtures for the CLI.

The CLI's resolvers reject platform handles at the user boundary: only
names cross normal command input. Some tests still exercise internal code
paths with pre-resolved handles; this autouse fixture short-circuits lookup
so those tests avoid live network calls while production code still enforces
the name-only contract.
"""

from __future__ import annotations

import os

import pytest

# Captured on the first autouse setup, before that fixture replaces it.
_REAL_SESSION_ACCOUNT_RESOLVER = None


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
    # Process-global like the transport flag, and for the same reason: it
    # remembers a session generation nothing could use, so a test that leaves
    # one set makes the next test's legitimate rebuild look futile.
    monkeypatch.setattr(web_session_module, "_unproven_rebuild", None)
    yield
    web_session_module._BROWSER_API_FORCE_BROWSER = False
    web_session_module._unproven_rebuild = None
    _close_browser_client()


@pytest.fixture(autouse=True)
def _isolate_web_session_storage(monkeypatch):  # noqa: ANN001
    """Give session storage no account to resolve unless a test names one.

    Everything the session layer writes — the cached session and the marker
    recording credentials CAS rejected — lands in the *active* account's
    directory. Left alone that is the real `~/.inspire/accounts/<current>/` of
    whoever runs pytest: a test that simulates a failed login would pause the
    developer's own next login for a minute, from a password that was never
    real. With no active account the resolver returns `None` and the whole
    layer becomes a no-op, which is what the tests that do not care about
    persistence want anyway.

    Tests that do care pass an explicit account and isolate `Path.home`
    themselves; the explicit name goes straight through this. Tests of the
    active-account resolution itself ask for `active_account_session_storage`.
    """
    global _REAL_SESSION_ACCOUNT_RESOLVER

    from inspire.platform.web.session import models as session_models

    if _REAL_SESSION_ACCOUNT_RESOLVER is None:
        _REAL_SESSION_ACCOUNT_RESOLVER = session_models._resolve_account_for_storage

    def _explicit_only(explicit):  # noqa: ANN001, ANN202
        return (explicit or "").strip() or None

    monkeypatch.setattr(session_models, "_resolve_account_for_storage", _explicit_only)


@pytest.fixture
def active_account_session_storage(monkeypatch):  # noqa: ANN001
    """Undo `_isolate_web_session_storage` for tests of the resolution itself.

    Only safe once the test has redirected `Path.home` or `$HOME`, which every
    caller of this does before asking for it.
    """
    from inspire.platform.web.session import models as session_models

    assert _REAL_SESSION_ACCOUNT_RESOLVER is not None
    monkeypatch.setattr(
        session_models,
        "_resolve_account_for_storage",
        _REAL_SESSION_ACCOUNT_RESOLVER,
    )


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
def _isolate_resource_index(monkeypatch, tmp_path):  # noqa: ANN001
    """Keep `ResourceIndex.for_account()` off the real `~/.inspire/`.

    Anything that resolves a name — every quota loader, every `--image`, every
    workload lookup — opens the index for the active account, and without a
    redirect that is the index belonging to whoever runs pytest. The suite then
    quietly depends on that machine's cache: `test_workload_quota_and_resources`
    stubbed the platform, but a developer whose real quota scope happened to be
    fresh got their own compute groups back instead of the stub, and the same
    test passed or failed depending on how recently they had run the CLI.

    Tests that want an index point `ResourceIndex` at their own path, which
    goes nowhere near this.
    """
    from inspire.cli.utils import resource_index as resource_index_module

    def _scratch_path(account=None):  # noqa: ANN001
        name = str(account or "").strip() or "default"
        return tmp_path / "resource-index" / name / resource_index_module.RESOURCE_INDEX_FILENAME

    monkeypatch.setattr(resource_index_module, "resource_index_path", _scratch_path)


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


def set_fake_home(monkeypatch, home) -> None:  # noqa: ANN001
    """Point ``Path.home()`` and ``~`` expansion at *home* on every platform.

    Setting ``HOME`` alone is a POSIX-only idiom: ``ntpath.expanduser`` consults
    ``USERPROFILE`` first and never looks at ``HOME``, so on Windows a test that
    sets only ``HOME`` silently reads and writes the real ``~/.inspire`` of
    whoever is running pytest — the exact hazard ``conftest``'s other fixtures
    exist to prevent.
    """
    home = str(home)
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)
    drive, tail = os.path.splitdrive(home)
    monkeypatch.setenv("HOMEDRIVE", drive)
    monkeypatch.setenv("HOMEPATH", tail or home)
