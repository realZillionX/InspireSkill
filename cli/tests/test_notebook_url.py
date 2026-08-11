"""Tests for notebook web-entry commands and IDE URL helpers."""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from inspire.cli.commands.notebook import url_cmd as url_cmd_mod
from inspire.cli.commands.notebook import notebook_lookup as notebook_lookup_module
from inspire.cli.commands.notebook.transport import NotebookTransportPolicy
from inspire.cli.context import Context
from inspire.cli.main import main as cli_main
from inspire.platform.web import browser_api as browser_api_mod
from inspire.platform.web.browser_api import playwright_notebooks as pw

_NOTEBOOK_ID = "bae66d5d-8423-4730-aa06-96a770748109"
_BASE_URL = "https://qz.sii.edu.cn"
_SUFFIX = (
    "/ws-6e6ba362-e98e-45b2-9c5a-311998e93d65"
    "/project-84370d52-6e91-4911-8116-5840a97e984c"
    "/user-263239cf-402f-4ae0-a8e2-2fcca034026c"
    f"/vscode/{_NOTEBOOK_ID}/ed659e4b-012e-4d94-9439-c67eebc771d5"
)
_GATEWAY = "https://nat2-notebook-inspire.sii.edu.cn"
# The gateway 302s to a *relative* target, so the canonical IDE URL must keep
# its trailing slash or the redirect drops the token segment and 404s.
_IDE_URL = f"{_GATEWAY}{_SUFFIX}/"
_IDE_URL_NO_SLASH = f"{_GATEWAY}{_SUFFIX}"
notebook_cli_module = importlib.import_module("inspire.cli.utils.notebook_cli")
workspace_module = importlib.import_module("inspire.config.workspaces")


# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------


def test_ide_gateway_url_keeps_host_and_strips_proxy_suffix() -> None:
    # A cached rtunnel proxy URL normalizes to the bare IDE gateway URL.
    proxy_url = f"{_IDE_URL_NO_SLASH}/proxy/8080/"
    assert pw._ide_gateway_url(proxy_url) == _IDE_URL


def test_ide_gateway_url_requires_a_host() -> None:
    assert pw._ide_gateway_url(_SUFFIX) is None  # host-less input
    assert pw._ide_gateway_url("") is None


def test_ide_gateway_url_always_ends_in_a_slash() -> None:
    """The gateway 302s to the relative target ``./?folder=...``.

    With the trailing slash ``./`` resolves to the token directory and the IDE
    loads; without it ``./`` resolves one level up, the ``<token>`` segment is
    dropped, and the redirect lands on a 404. Both forms answer 302 to the
    first request, so nothing catches this until the redirect is followed.
    """
    for variant in (
        _IDE_URL_NO_SLASH,  # what the iframe URL looks like before normalizing
        _IDE_URL,  # already canonical — normalizing must be idempotent
        f"{_IDE_URL_NO_SLASH}/lab?folder=/inspire/ssd/project/demo",
        f"{_IDE_URL_NO_SLASH}/proxy/8080/",
    ):
        assert pw._ide_gateway_url(variant) == _IDE_URL


def test_resolve_repairs_a_cached_url_that_lost_its_slash(monkeypatch) -> None:  # noqa: ANN001
    """Caches written before the slash was required must not survive a read.

    Such an entry still probes as live, because the gateway answers 302 to it
    just the same, so an unrepaired cache would keep handing back the URL that
    404s once the browser follows the redirect.
    """
    monkeypatch.setattr(pw, "_read_cached_ide_url", lambda *a, **k: _IDE_URL_NO_SLASH)
    monkeypatch.setattr(pw, "_warm_ide_url_candidates", lambda *a, **k: [])
    monkeypatch.setattr(pw, "_is_ide_url_live", lambda *a, **k: True)
    monkeypatch.setattr(pw, "_write_cached_ide_url", lambda *a, **k: None)
    monkeypatch.setattr(pw, "_active_account_name", lambda *a, **k: "acct")
    monkeypatch.setattr(pw, "_get_base_url", lambda: _BASE_URL)

    assert pw.resolve_notebook_vscode_ide_url(_NOTEBOOK_ID, session=object()) == _IDE_URL


def test_build_port_forward_url_keeps_ide_gateway_host() -> None:
    out = pw.build_notebook_port_forward_url(
        _IDE_URL,
        port=30000,
        service_path="/v1/models?limit=1",
    )
    assert out == (f"{_IDE_URL_NO_SLASH}/proxy/30000/v1/models?limit=1")


# ---------------------------------------------------------------------------
# _find_ide_gateway_url
# ---------------------------------------------------------------------------


class _FakeFrame:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakePage:
    def __init__(self, frame_urls: list[str], url: str = "") -> None:
        self.frames = [_FakeFrame(u) for u in frame_urls]
        self.url = url


def test_find_returns_full_ide_url_from_gateway_frame() -> None:
    page = _FakePage(["about:blank", f"{_IDE_URL_NO_SLASH}/lab"])
    assert pw._find_ide_gateway_url(page) == _IDE_URL


def test_find_returns_none_when_no_gateway_frame() -> None:
    page = _FakePage(["about:blank", "https://h/other"])
    assert pw._find_ide_gateway_url(page) is None


# ---------------------------------------------------------------------------
# resolve_notebook_vscode_ide_url — cache / probe / browser
# ---------------------------------------------------------------------------


def _patch_env(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(pw, "_get_base_url", lambda: _BASE_URL)
    monkeypatch.setattr(pw, "_active_account_name", lambda: None)
    monkeypatch.setattr(pw, "_write_cached_ide_url", lambda *a, **k: None)


def test_resolve_uses_cache_when_probe_live(monkeypatch) -> None:  # noqa: ANN001
    _patch_env(monkeypatch)
    monkeypatch.setattr(pw, "_read_cached_ide_url", lambda *a, **k: _IDE_URL)
    monkeypatch.setattr(pw, "_warm_ide_url_candidates", lambda *a, **k: [])
    monkeypatch.setattr(pw, "_is_ide_url_live", lambda *a, **k: True)

    def _no_browser(*a, **k):  # noqa: ANN002,ANN003
        raise AssertionError("browser must not run on a live cache hit")

    monkeypatch.setattr(pw, "resolve_notebook_ide_url", _no_browser)

    assert pw.resolve_notebook_vscode_ide_url(_NOTEBOOK_ID, session=object()) == _IDE_URL


def test_resolve_reuses_warm_rtunnel_candidate(monkeypatch) -> None:  # noqa: ANN001
    _patch_env(monkeypatch)
    monkeypatch.setattr(pw, "_read_cached_ide_url", lambda *a, **k: None)
    monkeypatch.setattr(pw, "_warm_ide_url_candidates", lambda *a, **k: [_IDE_URL])
    monkeypatch.setattr(pw, "_is_ide_url_live", lambda *a, **k: True)
    monkeypatch.setattr(
        pw,
        "resolve_notebook_ide_url",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("browser must not run")),
    )

    assert pw.resolve_notebook_vscode_ide_url(_NOTEBOOK_ID, session=object()) == _IDE_URL


def test_resolve_prefers_the_access_api_over_the_browser(monkeypatch) -> None:  # noqa: ANN001
    """A cache miss must cost one JSON call, not a headless Chromium.

    `GetNotebookAccessUrl` answers the same gateway URL in well under a second.
    """
    _patch_env(monkeypatch)
    monkeypatch.setattr(pw, "_read_cached_ide_url", lambda *a, **k: None)
    monkeypatch.setattr(pw, "_warm_ide_url_candidates", lambda *a, **k: [])
    monkeypatch.setattr(pw, "_ide_url_from_access_api", lambda *a, **k: _IDE_URL)
    monkeypatch.setattr(
        pw,
        "resolve_notebook_ide_url",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("browser must not run")),
    )

    assert pw.resolve_notebook_vscode_ide_url(_NOTEBOOK_ID, session=object()) == _IDE_URL


def test_resolve_still_consults_the_access_api_on_refresh(monkeypatch) -> None:  # noqa: ANN001
    """`refresh` means "do not trust the cache", not "insist on scraping"."""
    _patch_env(monkeypatch)
    monkeypatch.setattr(
        pw,
        "_read_cached_ide_url",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("cache read on refresh")),
    )
    monkeypatch.setattr(pw, "_ide_url_from_access_api", lambda *a, **k: _IDE_URL)
    monkeypatch.setattr(
        pw,
        "resolve_notebook_ide_url",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("browser must not run")),
    )

    out = pw.resolve_notebook_vscode_ide_url(_NOTEBOOK_ID, session=object(), refresh=True)
    assert out == _IDE_URL


def test_resolve_falls_back_to_browser_when_the_access_api_is_empty(monkeypatch) -> None:  # noqa: ANN001
    """A STOPPED notebook answers with two empty strings; the browser still tries."""
    _patch_env(monkeypatch)
    monkeypatch.setattr(pw, "_read_cached_ide_url", lambda *a, **k: None)
    monkeypatch.setattr(pw, "_warm_ide_url_candidates", lambda *a, **k: [])
    monkeypatch.setattr(pw, "_ide_url_from_access_api", lambda *a, **k: None)
    monkeypatch.setattr(pw, "resolve_notebook_ide_url", lambda *a, **k: _IDE_URL)

    assert pw.resolve_notebook_vscode_ide_url(_NOTEBOOK_ID, session=object()) == _IDE_URL


def test_access_api_returns_none_when_the_notebook_is_stopped(monkeypatch) -> None:  # noqa: ANN001
    import inspire.platform.web.browser_api.notebooks as notebooks_module

    monkeypatch.setattr(
        notebooks_module,
        "_notebook_v2",
        lambda *a, **k: {"jupyter_url": "", "vscode_url": ""},
    )
    assert pw._ide_url_from_access_api(_NOTEBOOK_ID, session=object()) is None


def test_access_api_normalizes_either_url_to_the_gateway_form(monkeypatch) -> None:  # noqa: ANN001
    """Both IDEs share one runtime and token, so either URL normalizes the same."""
    import inspire.platform.web.browser_api.notebooks as notebooks_module

    jupyter = _IDE_URL_NO_SLASH.replace("/vscode/", "/jupyter/") + "/lab?token=secret"
    monkeypatch.setattr(
        notebooks_module,
        "_notebook_v2",
        lambda *a, **k: {"jupyter_url": jupyter, "vscode_url": ""},
    )
    assert pw._ide_url_from_access_api(_NOTEBOOK_ID, session=object()) == _IDE_URL


def test_resolve_falls_back_to_browser_when_stale(monkeypatch) -> None:  # noqa: ANN001
    _patch_env(monkeypatch)
    monkeypatch.setattr(pw, "_read_cached_ide_url", lambda *a, **k: _IDE_URL)
    monkeypatch.setattr(pw, "_warm_ide_url_candidates", lambda *a, **k: [])
    monkeypatch.setattr(pw, "_is_ide_url_live", lambda *a, **k: False)  # token rotated
    monkeypatch.setattr(pw, "resolve_notebook_ide_url", lambda *a, **k: _IDE_URL)

    assert pw.resolve_notebook_vscode_ide_url(_NOTEBOOK_ID, session=object()) == _IDE_URL


def test_resolve_refresh_skips_cache_and_probe(monkeypatch) -> None:  # noqa: ANN001
    _patch_env(monkeypatch)
    monkeypatch.setattr(
        pw,
        "_read_cached_ide_url",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("cache read on refresh")),
    )
    monkeypatch.setattr(
        pw,
        "_is_ide_url_live",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("probe on refresh")),
    )
    monkeypatch.setattr(pw, "resolve_notebook_ide_url", lambda *a, **k: _IDE_URL)

    out = pw.resolve_notebook_vscode_ide_url(_NOTEBOOK_ID, session=object(), refresh=True)
    assert out == _IDE_URL


def test_resolve_returns_none_when_browser_fails(monkeypatch) -> None:  # noqa: ANN001
    _patch_env(monkeypatch)
    monkeypatch.setattr(pw, "_read_cached_ide_url", lambda *a, **k: None)
    monkeypatch.setattr(pw, "_warm_ide_url_candidates", lambda *a, **k: [])
    monkeypatch.setattr(pw, "resolve_notebook_ide_url", lambda *a, **k: None)

    assert pw.resolve_notebook_vscode_ide_url(_NOTEBOOK_ID, session=object()) is None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _patch_resolve(monkeypatch) -> list[str]:  # noqa: ANN001
    opened: list[str] = []
    monkeypatch.setattr(
        url_cmd_mod,
        "_resolve_notebook",
        lambda ctx, notebook, workspace, *, pick=None: (
            SimpleNamespace(storage_state={}),
            _BASE_URL,
            _NOTEBOOK_ID,
        ),
    )
    monkeypatch.setattr(
        url_cmd_mod,
        "preflight_notebook_transport_policy",
        lambda *_a, **_k: NotebookTransportPolicy(
            notebook="nb",
            notebook_id=_NOTEBOOK_ID,
            gpu_model="",
        ),
    )
    return opened


def test_resolve_notebook_validates_cached_handle_through_stale_retry(
    monkeypatch,
) -> None:  # noqa: ANN001
    session = SimpleNamespace(storage_state={})
    seen: dict[str, object] = {}
    detail_calls: list[str] = []

    monkeypatch.setattr(
        notebook_cli_module,
        "require_web_session",
        lambda *_args, **_kwargs: session,
    )
    monkeypatch.setattr(notebook_cli_module, "get_base_url", lambda: _BASE_URL)
    monkeypatch.setattr(notebook_cli_module, "load_config", lambda _ctx: SimpleNamespace())
    monkeypatch.setattr(
        workspace_module,
        "resolve_workspace_operation_scope",
        lambda *_args, **_kwargs: "ws-live",
    )

    def fake_retry(*_args, operation, **kwargs):  # noqa: ANN001
        seen.update(kwargs)
        return operation("notebook-live"), "notebook-live", "ws-live"

    monkeypatch.setattr(
        notebook_lookup_module,
        "_run_notebook_operation_with_stale_handle_retry",
        fake_retry,
    )
    monkeypatch.setattr(
        browser_api_mod,
        "get_notebook_detail",
        lambda *, notebook_id, session: detail_calls.append(notebook_id)
        or {"name": "demo-notebook"},
    )

    resolved_session, base_url, notebook_id = url_cmd_mod._resolve_notebook(
        Context(),
        "demo-notebook",
        "CPU资源空间",
        pick=2,
    )

    assert resolved_session is session
    assert base_url == _BASE_URL
    assert notebook_id == "notebook-live"
    assert seen["identifier"] == "demo-notebook"
    assert seen["workspace_ids"] == ["ws-live"]
    assert seen["pick"] == 2
    assert detail_calls == ["notebook-live"]


@pytest.mark.parametrize(
    ("command", "extra_args"),
    (("proxy-url", ("--port", "30000")),),
)
def test_url_commands_pass_pick_to_live_resolvers(
    monkeypatch,
    command: str,
    extra_args: tuple[str, ...],
) -> None:  # noqa: ANN001
    resolved_picks: list[int | None] = []
    policy_picks: list[int | None] = []
    monkeypatch.setattr(
        url_cmd_mod,
        "_resolve_notebook",
        lambda _ctx, _notebook, _workspace, *, pick=None: (
            resolved_picks.append(pick)
            or (
                SimpleNamespace(storage_state={}),
                _BASE_URL,
                _NOTEBOOK_ID,
            )
        ),
    )
    monkeypatch.setattr(
        url_cmd_mod,
        "preflight_notebook_transport_policy",
        lambda *_args, **kwargs: (
            policy_picks.append(kwargs.get("pick"))
            or NotebookTransportPolicy(
                notebook="nb",
                notebook_id=_NOTEBOOK_ID,
                gpu_model="",
            )
        ),
    )
    monkeypatch.setattr(
        browser_api_mod,
        "resolve_notebook_vscode_ide_url",
        lambda *_args, **_kwargs: _IDE_URL,
    )
    monkeypatch.setattr(
        browser_api_mod,
        "resolve_notebook_port_forward_url",
        lambda *_args, **_kwargs: _IDE_URL,
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "notebook",
            command,
            "nb",
            "--workspace",
            "CPU资源空间",
            "--pick",
            "2",
            *extra_args,
        ],
    )

    assert result.exit_code == 0, result.output
    assert resolved_picks == [2]
    assert policy_picks == [2]

def test_proxy_url_prints_the_url_as_its_whole_output(monkeypatch) -> None:  # noqa: ANN001
    """The one notebook command that must emit a platform URL.

    Agents have no browser; the address is the deliverable. It carries a token
    path segment, and there is no token-free form that proxies, so the scrub
    that every other notebook command applies is deliberately skipped here.
    """
    _patch_resolve(monkeypatch)
    monkeypatch.setattr(
        browser_api_mod,
        "resolve_notebook_port_forward_url",
        lambda *a, **k: f"{_IDE_URL_NO_SLASH}/proxy/{k['port']}{k['service_path']}",
    )
    result = CliRunner().invoke(
        cli_main,
        [
            "notebook",
            "proxy-url",
            "nb",
            "--workspace",
            "CPU资源空间",
            "--port",
            "30000",
            "--path",
            "/v1",
        ],
    )
    assert result.exit_code == 0
    assert result.output == f"{_IDE_URL_NO_SLASH}/proxy/30000/v1\n"


def test_proxy_url_json_carries_the_url(monkeypatch) -> None:  # noqa: ANN001
    _patch_resolve(monkeypatch)
    monkeypatch.setattr(
        browser_api_mod,
        "resolve_notebook_port_forward_url",
        lambda *a, **k: f"{_IDE_URL_NO_SLASH}/proxy/{k['port']}{k['service_path']}",
    )
    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "notebook",
            "proxy-url",
            "nb",
            "--workspace",
            "CPU资源空间",
            "--port",
            "30000",
            "--path",
            "/v1",
        ],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)["data"]
    assert data == {"name": "nb", "url": f"{_IDE_URL_NO_SLASH}/proxy/30000/v1"}
